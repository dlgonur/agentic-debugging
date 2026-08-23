from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_debugger.demo.catalog import build_reference_patch, scenario_for
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.evaluation.live import (
    LiveModelConfig,
    LiveRunLimits,
    run_live_case,
)
from agentic_debugger.events.replay import replay_events
from scripts import ollama_cloud_command_adapter


ROOT = Path(__file__).resolve().parents[2]
TASK_ID = "pdb-required-boundary-006"
LEVEL12_TASK_ID = "pdb-required-caller-callee-007"
LEVEL18_TASK_ID = "pdb-required-multistage-units-008"


class DeterministicConfiguredTransport:
    """A zero-provider JSON transport exercising the real live adapter."""

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def request(self, payload, timeout_seconds):
        del timeout_seconds
        self.requests.append(json.loads(json.dumps(payload)))
        directives = [
            {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}},
            {"kind": "transition", "target_state": "Understand", "reason": "baseline reproduced"},
            {"kind": "action", "name": "get_source_window", "arguments": {"path": "window_tail.py", "line": 1}},
            {"kind": "add_hypothesis", "hypothesis_id": "proof-boundary-006", "statement": scenario_for(TASK_ID).root_cause_statement, "confidence": "low", "evidence_refs": ["observation:find_function", "observation:get_source_window"], "requires_runtime_evidence": True},
            {"kind": "transition", "target_state": "RuntimeEvidence", "reason": "exact PDB proof is required"},
            {"kind": "action", "name": "start_pdb_session", "arguments": {"breakpoint_line": 9}},
            {"kind": "action", "name": "get_stack_summary", "arguments": {}},
            {"kind": "action", "name": "get_frame_locals", "arguments": {"frame_id": 0, "pause_generation": 1}},
            {"kind": "action", "name": "next_pdb_session", "arguments": {}},
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
        logical_index = payload["protocol"]["logical_model_call_index"]
        directive = directives[logical_index]
        if directive is not None:
            return directive

        controller = payload["controller"]
        history = payload["history"]
        observations = [
            entry.get("last_observation")
            for entry in history
            if isinstance(entry, dict) and isinstance(entry.get("last_observation"), dict)
        ]
        current_observation = controller.get("last_observation")
        if isinstance(current_observation, dict):
            observations.append(current_observation)
        unique_observations = {}
        for observation in observations:
            observation_id = observation.get("observation_id")
            if isinstance(observation_id, str):
                unique_observations[observation_id] = observation
        proof_observations = [
            entry for entry in unique_observations.values()
            if entry.get("name") in {"start_pdb_session", "get_stack_summary", "get_frame_locals", "next_pdb_session"}
        ]
        refs = [entry["observation_id"] for entry in proof_observations]
        if logical_index == 11:
            return {"kind": "revise_hypothesis", "hypothesis_id": "proof-boundary-006", "statement": scenario_for(TASK_ID).root_cause_statement, "confidence": "low", "evidence_refs": refs, "requires_runtime_evidence": False}
        locals_observation = next(entry for entry in proof_observations if entry["name"] == "get_frame_locals")
        requested = next(item for item in locals_observation["payload"]["locals"] if item["name"] == "requested_size")
        return {"kind": "action", "name": "express_root_cause_hypothesis", "arguments": {"hypothesis_id": "proof-boundary-006", "statement": scenario_for(TASK_ID).root_cause_statement, "target_file": "window_tail.py", "target_symbol": "tail_window", "confidence": "low", "evidence_refs": refs, "observed_values": {"requested_size": requested["value"]}}}


class ExitedCycleRecoveryTransport:
    """Exercise an exited control followed by a fresh exact-PDB cycle."""

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def request(self, payload, timeout_seconds):
        del timeout_seconds
        self.requests.append(json.loads(json.dumps(payload)))
        index = payload["protocol"]["logical_model_call_index"]
        fixed = {
            0: {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}},
            1: {"kind": "transition", "target_state": "Understand", "reason": "baseline reproduced"},
            2: {"kind": "action", "name": "get_source_window", "arguments": {"path": "window_tail.py", "line": 1}},
            3: {"kind": "add_hypothesis", "hypothesis_id": "proof-boundary-006", "statement": scenario_for(TASK_ID).root_cause_statement, "confidence": "low", "evidence_refs": ["observation:get_source_window"], "requires_runtime_evidence": True},
            4: {"kind": "transition", "target_state": "RuntimeEvidence", "reason": "exact runtime proof required"},
            5: {"kind": "action", "name": "start_pdb_session", "arguments": {"breakpoint_line": 10}},
            6: {"kind": "action", "name": "get_stack_summary", "arguments": {}},
            7: {"kind": "action", "name": "get_frame_locals", "arguments": {"frame_id": 0, "pause_generation": 1}},
            8: {"kind": "action", "name": "next_pdb_session", "arguments": {}},
            9: {"kind": "action", "name": "start_pdb_session", "arguments": {"breakpoint_line": 9}},
            10: {"kind": "action", "name": "get_stack_summary", "arguments": {}},
            11: {"kind": "action", "name": "get_frame_locals", "arguments": {"frame_id": 0, "pause_generation": 1}},
            12: {"kind": "action", "name": "next_pdb_session", "arguments": {}},
            13: {"kind": "action", "name": "stop_pdb_session", "arguments": {}},
            14: {"kind": "transition", "target_state": "Understand", "reason": "fresh proof cycle complete"},
            17: {"kind": "transition", "target_state": "Patch", "reason": "diagnosis is evidence-bound"},
            19: {"kind": "action", "name": "syntax_check", "arguments": {}},
            20: {"kind": "transition", "target_state": "Validate", "reason": "candidate syntax is valid"},
            21: {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "post_patch"}},
            22: {"kind": "action", "name": "run_regression_tests", "arguments": {}},
            23: {"kind": "action", "name": "classify_outcome", "arguments": {}},
            24: {"kind": "transition", "target_state": "Done", "reason": "independent validation complete"},
        }
        if index in fixed:
            return fixed[index]
        if index == 15:
            constraints = payload["directive_schema"]["revise_hypothesis"]["constraints"]
            return {
                "kind": "revise_hypothesis",
                "hypothesis_id": "proof-boundary-006",
                "statement": scenario_for(TASK_ID).root_cause_statement,
                "confidence": "low",
                "evidence_refs": constraints["evidence_refs"]["example"],
                "requires_runtime_evidence": False,
            }
        if index == 16:
            properties = payload["action_contracts"]["express_root_cause_hypothesis"]["properties"]
            return {
                "kind": "action",
                "name": "express_root_cause_hypothesis",
                "arguments": {
                    "hypothesis_id": "proof-boundary-006",
                    "statement": scenario_for(TASK_ID).root_cause_statement,
                    "target_file": "window_tail.py",
                    "target_symbol": "tail_window",
                    "confidence": "low",
                    "evidence_refs": properties["evidence_refs"]["example"],
                    "observed_values": properties["observed_values"]["example"],
                },
            }
        if index == 18:
            fixture = ROOT / "agentic_debugger/datasets/curated" / TASK_ID
            scenario = scenario_for(TASK_ID)
            patch = build_reference_patch(
                (fixture / scenario.reference_repair.target_path).read_text(encoding="utf-8"),
                scenario.reference_repair,
            )
            return {"kind": "action", "name": "apply_patch", "arguments": {"patch": patch}}
        raise AssertionError(f"unexpected recovery request index {index}")


class LadderConfiguredTransport(DeterministicConfiguredTransport):
    """Reuse the accepted exact-proof sequence for one ladder fixture."""

    def __init__(self, task_id: str, observed_names: set[str]) -> None:
        super().__init__()
        self.task_id = task_id
        self.scenario = scenario_for(task_id)
        self.observed_names = observed_names

    def request(self, payload, timeout_seconds):
        logical_index = payload["protocol"]["logical_model_call_index"]
        if logical_index in {11, 12}:
            del timeout_seconds
            self.requests.append(json.loads(json.dumps(payload)))
            observations = [
                entry.get("last_observation")
                for entry in payload["history"]
                if isinstance(entry, dict) and isinstance(entry.get("last_observation"), dict)
            ]
            current = payload["controller"].get("last_observation")
            if isinstance(current, dict):
                observations.append(current)
            unique = {
                item["observation_id"]: item
                for item in observations
                if isinstance(item.get("observation_id"), str)
            }
            proof = [
                item for item in unique.values()
                if item.get("name") in {
                    "start_pdb_session", "get_stack_summary",
                    "get_frame_locals", "next_pdb_session",
                }
            ]
            refs = [item["observation_id"] for item in proof]
            statement = self.scenario.root_cause_statement
            if logical_index == 11:
                return {
                    "kind": "revise_hypothesis",
                    "hypothesis_id": self.scenario.hypothesis_id,
                    "statement": statement,
                    "confidence": "low",
                    "evidence_refs": refs,
                    "requires_runtime_evidence": False,
                }
            locals_observation = next(item for item in proof if item["name"] == "get_frame_locals")
            visible = {
                item["name"]: item["value"]
                for item in locals_observation["payload"]["locals"]
                if item["name"] in self.observed_names
            }
            return {
                "kind": "action",
                "name": "express_root_cause_hypothesis",
                "arguments": {
                    "hypothesis_id": self.scenario.hypothesis_id,
                    "statement": statement,
                    "target_file": self.scenario.localization.file_path,
                    "target_symbol": self.scenario.localization.symbol,
                    "confidence": "low",
                    "evidence_refs": refs,
                    "observed_values": visible,
                },
            }

        directive = super().request(payload, timeout_seconds)
        encoded = json.dumps(directive)
        encoded = encoded.replace("proof-boundary-006", self.scenario.hypothesis_id)
        encoded = encoded.replace(
            scenario_for(TASK_ID).root_cause_statement,
            self.scenario.root_cause_statement,
        )
        encoded = encoded.replace("window_tail.py", self.scenario.localization.file_path)
        encoded = encoded.replace("tail_window", self.scenario.localization.symbol)
        if logical_index == 5:
            encoded = encoded.replace(
                '"breakpoint_line": 9',
                f'"breakpoint_line": {self.scenario.runtime_probe.breakpoint_line}',
            )
        if logical_index == 14:
            fixture = ROOT / "agentic_debugger" / "datasets" / "curated" / self.task_id
            scenario = self.scenario
            patch = build_reference_patch(
                (fixture / scenario.reference_repair.target_path).read_text(encoding="utf-8"),
                scenario.reference_repair,
            )
            return {"kind": "action", "name": "apply_patch", "arguments": {"patch": patch}}
        return json.loads(encoded)


class AdversarialConfiguredTransport:
    """Follow the failed-run branch only when the request exposes it."""

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.legacy_branch_seen = False
        self._phase = 0
        self._source_seen = False
        fixture = ROOT / "agentic_debugger" / "datasets" / "curated" / TASK_ID
        scenario = scenario_for(TASK_ID)
        self.patch = build_reference_patch(
            (fixture / scenario.reference_repair.target_path).read_text(encoding="utf-8"),
            scenario.reference_repair,
        )

    @staticmethod
    def _observations(payload):
        values = [
            item.get("last_observation")
            for item in payload.get("history", [])
            if isinstance(item, dict) and isinstance(item.get("last_observation"), dict)
        ]
        current = payload.get("controller", {}).get("last_observation")
        if isinstance(current, dict):
            values.append(current)
        unique = {}
        for value in values:
            if isinstance(value.get("observation_id"), str):
                unique[value["observation_id"]] = value
        return list(unique.values())

    def request(self, payload, timeout_seconds):
        del timeout_seconds
        self.requests.append(json.loads(json.dumps(payload)))
        allowed = set(payload["controller"]["allowed_actions"])
        state = payload["controller"]["state"]
        observations = self._observations(payload)
        if self._phase == 0:
            self._phase += 1
            return {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}
        if self._phase == 1:
            self._phase = 3
            return {"kind": "transition", "target_state": "Understand", "reason": "baseline evidence is recorded"}

        if state == "Understand" and "express_root_cause_hypothesis" in allowed and self._phase == 3:
            self.legacy_branch_seen = True
            return {"kind": "action", "name": "express_root_cause_hypothesis", "arguments": {"hypothesis_id": "adversarial", "statement": "boundary behavior requires runtime confirmation", "target_file": "window_tail.py", "target_symbol": "tail_window", "confidence": "low", "evidence_refs": [], "observed_values": {}}}
        if self._phase == 3 and "get_source_window" in allowed and not self._source_seen:
            self._source_seen = True
            return {"kind": "action", "name": "get_source_window", "arguments": {"path": "window_tail.py", "line": 1}}
        if self._phase == 3:
            self._phase += 1
            return {"kind": "add_hypothesis", "hypothesis_id": "adversarial", "statement": "boundary behavior requires runtime confirmation", "confidence": "low", "evidence_refs": [], "requires_runtime_evidence": True}
        if self._phase == 4:
            self._phase += 1
            return {"kind": "transition", "target_state": "RuntimeEvidence", "reason": "runtime evidence is required"}
        if self._phase == 5:
            self._phase += 1
            return {"kind": "action", "name": "start_pdb_session", "arguments": {"breakpoint_line": 9}}
        if self._phase == 6:
            self._phase += 1
            return {"kind": "action", "name": "get_stack_summary", "arguments": {}}
        if self._phase == 7:
            self._phase += 1
            return {"kind": "action", "name": "get_frame_locals", "arguments": {"frame_id": 0, "pause_generation": 1}}
        if self._phase == 8:
            self._phase += 1
            return {"kind": "action", "name": "next_pdb_session", "arguments": {}}
        if self._phase == 9:
            self._phase += 1
            return {"kind": "action", "name": "stop_pdb_session", "arguments": {}}
        if self._phase == 10:
            self._phase += 1
            return {"kind": "transition", "target_state": "Understand", "reason": "bounded observations are complete"}
        if self._phase == 11:
            self._phase += 1
            refs = [item["observation_id"] for item in observations if item.get("name") in {"get_stack_summary", "get_frame_locals", "next_pdb_session"}]
            return {"kind": "revise_hypothesis", "hypothesis_id": "adversarial", "statement": "boundary behavior is bound to the observed runtime state", "confidence": "low", "evidence_refs": refs, "requires_runtime_evidence": False}
        if self._phase == 12:
            self._phase += 1
            start = next(item for item in observations if item.get("name") == "start_pdb_session")
            locals_observation = next(item for item in observations if item.get("name") == "get_frame_locals")
            values = locals_observation["payload"]["locals"]
            observed_values = {values[0]["name"]: values[0]["value"]}
            refs = [item["observation_id"] for item in observations if item.get("name") in {"get_stack_summary", "get_frame_locals", "next_pdb_session"}]
            return {"kind": "action", "name": "express_root_cause_hypothesis", "arguments": {"hypothesis_id": "adversarial", "statement": "the implementation violates the observed boundary behavior", "target_file": start["payload"]["proof"]["production_file"], "target_symbol": start["payload"]["proof"]["production_frame"], "confidence": "low", "evidence_refs": refs, "observed_values": observed_values}}
        if self._phase == 13:
            self._phase += 1
            return {"kind": "transition", "target_state": "Patch", "reason": "bound diagnosis is complete"}
        if self._phase == 14:
            self._phase += 1
            return {"kind": "action", "name": "apply_patch", "arguments": {"patch": self.patch}}
        if self._phase == 15:
            self._phase += 1
            return {"kind": "action", "name": "syntax_check", "arguments": {}}
        if self._phase == 16:
            self._phase += 1
            return {"kind": "transition", "target_state": "Validate", "reason": "candidate syntax is valid"}
        if self._phase == 17:
            self._phase += 1
            return {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "post_patch"}}
        if self._phase == 18:
            self._phase += 1
            return {"kind": "action", "name": "run_regression_tests", "arguments": {}}
        if self._phase == 19:
            self._phase += 1
            return {"kind": "action", "name": "classify_outcome", "arguments": {}}
        return {"kind": "transition", "target_state": "Done", "reason": "independent validation evidence is complete"}


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


def test_exited_exact_control_releases_session_and_fresh_cycle_resolves(tmp_path: Path) -> None:
    transport = ExitedCycleRecoveryTransport()
    result = run_live_case(
        repository_root=str(ROOT),
        task_id=TASK_ID,
        policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        repetition=1,
        workspace_parent=str(tmp_path),
        config=LiveModelConfig("recovery-zero-provider", ("synthetic-zero-provider",)),
        limits=LiveRunLimits(
            max_model_requests=28,
            max_controller_steps=28,
            max_model_phase_seconds=600,
            max_retries=0,
            continue_on_task_failure=False,
        ),
        transport=transport,
        evaluation_id="synthetic-exited-cycle-recovery",
        retain_observable_model_directives=True,
    )

    assert result.status.value == "RESOLVED"
    replay = replay_events(result.events_jsonl)
    observations = [
        event.payload["observation"]
        for event in replay.events
        if event.event_type.value == "observation"
    ]
    starts = [item for item in observations if item["name"] == "start_pdb_session"]
    controls = [item for item in observations if item["name"] == "next_pdb_session"]
    assert [item["status"] for item in starts] == ["ok", "ok"]
    assert [item["payload"]["proof"]["breakpoint_line"] for item in starts] == [10, 9]
    assert controls[0]["payload"]["state"] == "exited"
    assert controls[0]["payload"]["session_released"] is True
    assert controls[1]["payload"]["state"] == "paused"
    assert result.evidence["proof_cycle_events"] == [{
        "schema_version": "pdb-proof-cycle-event-v1",
        "event": "selected_runtime_cycle_reset",
        "trigger_observation_id": controls[0]["observation_id"],
        "trigger_action": "next_pdb_session",
        "trigger_state": "exited",
        "reason": "control did not pause in the declared production frame and the session is inactive",
        "removed_selected_observation_ids": [
            starts[0]["observation_id"],
            next(item["observation_id"] for item in observations[:observations.index(controls[0])] if item["name"] == "get_stack_summary"),
            next(item["observation_id"] for item in observations[:observations.index(controls[0])] if item["name"] == "get_frame_locals"),
        ],
        "trigger_retained_in_trajectory": True,
    }]
    assert result.verifier["outcome"] == "RESOLVED"
    assert result.reporting["cleanup"] == "cleaned"


@pytest.mark.parametrize(
    ("task_id", "observed_names", "evaluation_id"),
    [
        (
            LEVEL12_TASK_ID,
            {"caller_amount", "caller_representation"},
            "synthetic-level12-proof",
        ),
        (
            LEVEL18_TASK_ID,
            {"value", "base_delay_ms", "retry_count"},
            "synthetic-level18-proof",
        ),
    ],
)
def test_ladder_fixture_resolves_through_the_real_exact_pdb_live_path(
    tmp_path: Path,
    task_id: str,
    observed_names: set[str],
    evaluation_id: str,
) -> None:
    transport = LadderConfiguredTransport(task_id, observed_names)
    result = run_live_case(
        repository_root=str(ROOT),
        task_id=task_id,
        policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        repetition=1,
        workspace_parent=str(tmp_path),
        config=LiveModelConfig("deterministic-configured-model", ("synthetic-zero-provider",)),
        limits=LiveRunLimits(
            max_model_requests=24,
            max_controller_steps=24,
            max_model_phase_seconds=3600,
            max_retries=0,
            continue_on_task_failure=False,
        ),
        transport=transport,
        evaluation_id=evaluation_id,
        retain_observable_model_directives=True,
    )

    assert result.status.value == "RESOLVED", (
        result.diagnostics,
        result.controller,
        result.measurements,
    )
    assert result.verifier["outcome"] == "RESOLVED"
    assert result.verifier["canonical_fixture_unchanged"] is True
    assert result.verifier["workspace_cleaned"] is True
    assert result.measurements["logical_model_call_count"] == 21
    assert result.measurements["transport_attempt_count"] == 21
    assert result.measurements["retry_count"] == 0
    assert result.measurements["provider_error_count"] == 0
    canonical_sizes = [
        len(ollama_cloud_command_adapter.canonical_public_request(request).encode("utf-8"))
        for request in transport.requests
    ]
    assert max(canonical_sizes) <= ollama_cloud_command_adapter.MAX_PUBLIC_REQUEST_BYTES
    provider_visible = json.dumps(transport.requests, sort_keys=True).lower()
    assert "oracle" not in provider_visible
    assert "private_verifier" not in provider_visible
    assert "reference_repair" not in provider_visible
    assert replay_events(result.events_jsonl).events


def test_adversarial_live_case_cannot_take_the_old_impossible_diagnosis_branch(tmp_path: Path) -> None:
    transport = AdversarialConfiguredTransport()
    result = run_live_case(
        repository_root=str(ROOT),
        task_id=TASK_ID,
        policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        repetition=1,
        workspace_parent=str(tmp_path),
        config=LiveModelConfig("adversarial-configured-model", ("synthetic-zero-provider",)),
        limits=LiveRunLimits(
            max_model_requests=24,
            max_controller_steps=24,
            max_model_phase_seconds=600,
            max_retries=0,
            continue_on_task_failure=False,
        ),
        transport=transport,
        evaluation_id="adversarial-proof",
        retain_observable_model_directives=True,
    )

    assert result.status.value == "RESOLVED"
    assert transport.legacy_branch_seen is False
    assert result.measurements["logical_model_call_count"] <= 24
    assert result.measurements["transport_attempt_count"] <= 24
    assert result.measurements["retry_count"] == 0
    assert result.measurements["provider_error_count"] == 0
    assert result.measurements["max_request_bytes"] <= ollama_cloud_command_adapter.MAX_PUBLIC_REQUEST_BYTES
    assert result.verifier["canonical_fixture_unchanged"] is True
    assert result.verifier["workspace_cleaned"] is True
    assert replay_events(result.events_jsonl).events

    canonical_sizes = []
    for request in transport.requests:
        canonical_sizes.append(len(ollama_cloud_command_adapter.canonical_public_request(request).encode("utf-8")))
        ollama_cloud_command_adapter.build_chat_messages(request)
    assert canonical_sizes
    assert max(canonical_sizes) <= ollama_cloud_command_adapter.MAX_PUBLIC_REQUEST_BYTES
    provider_visible = json.dumps(transport.requests, sort_keys=True).lower()
    assert "oracle" not in provider_visible
    assert "private_verifier" not in provider_visible
    assert "reference_repair" not in provider_visible


def test_real_live_case_uses_zero_provider_and_completes_exact_pdb_proof(tmp_path: Path) -> None:
    result, transport = run_synthetic_live_case(tmp_path)
    assert result.status.value == "RESOLVED"
    assert result.controller["completed"] is True
    assert result.verifier["outcome"] == "RESOLVED"
    assert result.verifier["canonical_fixture_unchanged"] is True
    assert result.verifier["workspace_cleaned"] is True
    measurements = result.measurements
    assert measurements["logical_model_call_count"] == 21
    assert measurements["transport_attempt_count"] == 21
    assert measurements["logical_model_call_count"] == len(transport.requests)
    assert measurements["logical_model_call_count"] <= 24
    assert measurements["transport_attempt_count"] <= 24
    assert measurements["cumulative_request_bytes"] <= 600_000
    canonical_sizes = []
    provider_messages = []
    for request in transport.requests:
        canonical = ollama_cloud_command_adapter.canonical_public_request(request)
        provider_messages.append(ollama_cloud_command_adapter.build_chat_messages(request))
        canonical_sizes.append(len(canonical.encode("utf-8")))
    assert len(canonical_sizes) == 21
    assert len(provider_messages) == 21
    assert max(canonical_sizes) <= ollama_cloud_command_adapter.MAX_PUBLIC_REQUEST_BYTES
    assert measurements["max_request_bytes"] == max(len(json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")) for request in transport.requests)
    assert measurements["retry_count"] == 0
    assert measurements["provider_error_count"] == 0
    assert measurements["termination_reason"] is None
    assert measurements["controller_wall_duration_ms"] >= 0
    assert measurements["verifier_wall_duration_ms"] >= 0

    runtime_requests = [
        request
        for request in transport.requests
        if request["controller"]["state"] == "RuntimeEvidence"
    ]
    assert all(
        ("proof_gate" in request)
        == (request["controller"]["state"] == "RuntimeEvidence")
        for request in transport.requests
    )
    assert "run the advertised baseline once" in transport.requests[0]["instructions"]
    assert transport.requests[1]["controller"]["state"] == "Reproduce"
    assert transport.requests[1]["controller"]["allowed_actions"] == []
    assert transport.requests[1]["controller"]["legal_transition_targets"] == ["Understand"]
    assert "baseline recorded" in transport.requests[1]["instructions"]
    assert list(transport.requests[2]["directive_schema"]) == ["action"]
    source_contract = transport.requests[2]["action_contracts"]["get_source_window"][
        "properties"
    ]
    assert source_contract["path"]["enum"] == ["window_tail.py"]
    assert source_contract["line"]["enum"] == [1]
    assert transport.requests[2]["controller"]["allowed_actions"] == ["get_source_window"]
    assert list(transport.requests[3]["directive_schema"]) == ["add_hypothesis"]
    assert transport.requests[3]["controller"]["allowed_actions"] == []
    assert transport.requests[3]["directive_schema"]["add_hypothesis"]["constraints"][
        "requires_runtime_evidence"
    ]["enum"] == [True]
    assert list(transport.requests[4]["directive_schema"]) == ["transition"]
    assert transport.requests[4]["controller"]["allowed_actions"] == []
    assert transport.requests[4]["controller"]["legal_transition_targets"] == ["RuntimeEvidence"]
    assert list(transport.requests[11]["directive_schema"]) == ["revise_hypothesis"]
    assert transport.requests[11]["directive_schema"]["revise_hypothesis"]["constraints"][
        "requires_runtime_evidence"
    ]["enum"] == [False]
    revise_constraints = transport.requests[11]["directive_schema"]["revise_hypothesis"][
        "constraints"
    ]
    assert revise_constraints["hypothesis_id"]["enum"] == ["proof-boundary-006"]
    assert len(revise_constraints["evidence_refs"]["example"]) == 4
    assert transport.requests[11]["controller"]["allowed_actions"] == []
    assert transport.requests[11]["controller"]["legal_transition_targets"] == []
    assert list(transport.requests[12]["directive_schema"]) == ["action"]
    assert transport.requests[12]["controller"]["allowed_actions"] == ["express_root_cause_hypothesis"]
    assert transport.requests[12]["controller"]["legal_transition_targets"] == []
    locals_contract = transport.requests[7]["action_contracts"]["get_frame_locals"][
        "properties"
    ]
    assert locals_contract["frame_id"]["enum"] == [0]
    assert locals_contract["pause_generation"]["enum"] == [1]
    diagnosis_contract = transport.requests[12]["action_contracts"][
        "express_root_cause_hypothesis"
    ]["properties"]
    assert diagnosis_contract["hypothesis_id"]["enum"] == ["proof-boundary-006"]
    assert len(diagnosis_contract["evidence_refs"]["example"]) == 4
    assert diagnosis_contract["observed_values"]["example"]
    assert list(transport.requests[13]["directive_schema"]) == ["transition"]
    assert transport.requests[13]["controller"]["allowed_actions"] == []
    assert transport.requests[13]["controller"]["legal_transition_targets"] == ["Patch"]
    patch_transition_guidance = ollama_cloud_command_adapter.build_request_guidance(
        transport.requests[13]
    )
    assert '"target_state":"Patch"' in patch_transition_guidance
    assert '"target_state":"<one of Patch>"' not in patch_transition_guidance
    patch_guidance = ollama_cloud_command_adapter.build_request_guidance(
        transport.requests[14]
    )
    assert "JSON escape \\n" in patch_guidance
    assert "Current public PDB/source evidence binds the diagnosed line to window_tail.py:9." in patch_guidance
    assert "normal context-bearing hunk" in patch_guidance
    assert "zero-context one-line hunk" not in patch_guidance
    assert [set(request["controller"]["allowed_actions"]) for request in runtime_requests] == [
        {"start_pdb_session"},
        {"get_stack_summary"},
        {"get_frame_locals"},
        {"next_pdb_session"},
        {"stop_pdb_session"},
        set(),
    ]
    assert all("proof_gate" in request for request in runtime_requests)
    assert [list(request["directive_schema"]) for request in runtime_requests] == [
        ["action"],
        ["action"],
        ["action"],
        ["action"],
        ["action"],
        ["transition"],
    ]
    breakpoint_contract = runtime_requests[0]["action_contracts"][
        "start_pdb_session"
    ]["properties"]["breakpoint_line"]
    assert breakpoint_contract["type"] == "integer"
    assert breakpoint_contract["minimum"] == 1
    assert "enum" not in breakpoint_contract
    assert all(
        "continue_pdb_session" not in request["controller"]["allowed_actions"]
        for request in runtime_requests
    )
    assert all(
        request["controller"]["legal_transition_targets"] == []
        for request in runtime_requests[:-1]
    )
    assert runtime_requests[-1]["proof_gate"]["pre_diagnosis_ready"] is True
    assert runtime_requests[-1]["proof_gate"]["session_active"] is False
    assert "Understand" in runtime_requests[-1]["controller"]["legal_transition_targets"]
    assert runtime_requests[-1]["controller"]["legal_transition_targets"] == ["Understand"]

    transition_request_pairs = [
        (request, request.get("controller", {}).get("legal_transition_targets", []), directive)
        for request, directive in zip(transport.requests, [entry.get("directive") for entry in result.evidence["observable_model_directives"]])
    ]
    runtime_request = next(item for item in transition_request_pairs if item[2].get("target_state") == "RuntimeEvidence")
    assert "Patch" not in runtime_request[1]
    patch_request = next(item for item in transition_request_pairs if item[2].get("target_state") == "Patch")
    assert "Patch" in patch_request[1]
    assert "apply_patch" not in runtime_request[0]["controller"]["allowed_actions"]

    diagnosis_index = next(index for index, entry in enumerate(result.evidence["observable_model_directives"]) if entry["directive"].get("name") == "express_root_cause_hypothesis")
    diagnosis_request = transport.requests[diagnosis_index]
    diagnosis_observations = [
        entry["last_observation"]
        for entry in diagnosis_request["history"]
        if isinstance(entry, dict) and isinstance(entry.get("last_observation"), dict)
    ]
    diagnosis_observations.append(diagnosis_request["controller"]["last_observation"])
    diagnosis_names = {observation["name"] for observation in diagnosis_observations}
    assert {"get_stack_summary", "get_frame_locals"} <= diagnosis_names
    assert {"step_pdb_session", "next_pdb_session"} & diagnosis_names
    provider_visible = json.dumps(transport.requests, sort_keys=True)
    assert "oracle" not in provider_visible
    assert "private_verifier" not in provider_visible
    assert "reference_repair" not in provider_visible
