from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from scripts import ollama_cloud_command_adapter as cloud_adapter
from scripts import run_cookiecutter_967_pdb_proof as level32_operator

from agentic_debugger.agent.controller_policy import (
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisConfidence,
    HypothesisLedger,
    HypothesisStatus,
    RootCauseHypothesis,
)
from agentic_debugger.agent.model_adapter import (
    ActionDirective,
    ControllerSnapshot,
    ReviseHypothesisDirective,
    TransitionDirective,
)
from agentic_debugger.agent.proof_gate import (
    PROOF_ROLE_SELECTION_POLICY,
    PROOF_ROLE_SELECTION_POLICY_ID,
    PROOF_ROLE_SELECTION_SCHEMA_VERSION,
    validate_pdb_patch_evidence,
    validate_pdb_runtime_evidence,
)
from agentic_debugger.agent.state_machine import ControllerState, is_transition_allowed
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.evaluation.directive_observability import (
    export_rejection_evidence,
    serialize_rejection_evidence,
    validate_rejection_evidence,
)
from agentic_debugger.evaluation.live import (
    DIRECTIVE_NORMALIZATION_POLICY_ID,
    DIRECTIVE_NORMALIZATION_SCHEMA_VERSION,
    PDB_BREAKPOINT_SELECTION_POLICY_ID,
    PDB_BREAKPOINT_SELECTION_SCHEMA_VERSION,
    LiveModelAdapter,
    LiveModelConfig,
    LiveModelAdapterError,
    LiveRunLimits,
    LiveTreatmentBudget,
    LiveCaseStatus,
    _resolve_provider_directive,
    _parse,
    run_live_case,
)
from agentic_debugger.evaluation.task_schema import DebugTask
from agentic_debugger.agent.tool_registry import ToolRegistry, ToolSpec, ToolResult
from agentic_debugger.agent.controller_policy import ActionName
from agentic_debugger.events.schema import Action, Observation, ObservationStatus
from agentic_debugger.events.replay import replay_events

ROOT = Path(__file__).resolve().parents[2]
TASK_ID = "curated-none-handling-001"


def _task():
    return DebugTask.from_mapping(json.loads((ROOT / "agentic_debugger/datasets/curated" / TASK_ID / "task.json").read_text()))


def _registry():
    return ToolRegistry((ToolSpec(
        ActionName.RUN_REPRODUCTION,
        lambda arguments: dict(arguments),
        lambda _action, _arguments: ToolResult(ObservationStatus.OK, {}, "ok"),
        argument_contract={"required": ["phase"], "properties": {"phase": {"type": "string", "min_length": 1}}, "additional_properties": False},
    ),))


def _snapshot(task, limits):
    return ControllerSnapshot("level32-test", task.task_id, ControllerState.REPRODUCE, 0, limits, ControllerBudgetState(), HypothesisLedger())


def _adapter(response, *, limits=None, visible_limits=None, visible_task=None):
    class Transport:
        def __init__(self):
            self.requests = []

        def request(self, payload, timeout_seconds):
            self.requests.append(payload)
            return response

    transport = Transport()
    task = _task()
    adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.STATIC_BASELINE,
        config=LiveModelConfig("test-model", ("test-model-command",)),
        transport=transport,
        limits=limits or LiveRunLimits(max_model_requests=1, max_retries=0),
        registry=_registry(),
        model_visible_budget_limits=visible_limits,
        model_visible_task=visible_task,
    )
    return adapter, transport, task


def test_provider_completion_is_canonical_live_parser_input_and_counts_usage():
    content = json.dumps({"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}})
    adapter, _transport, task = _adapter({"provider_completion_schema_version": "provider-completion-v1", "directive_content": content, "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}, "transport_activity": {"stream_frame_count": 2, "thinking_bytes": 41, "content_bytes": len(content.encode())}})
    directive = adapter.next_directive(_snapshot(task, ControllerBudgetLimits.from_task_constraints(task.constraints)))
    assert isinstance(directive, ActionDirective)
    assert adapter.metrics.model_responses == 1
    assert adapter.metrics.to_mapping()["token_usage"]["total_tokens"] == 5
    attempt = adapter.directive_attempts[0]
    assert attempt["provider_transport_completed"] is True
    assert attempt["directive_accepted"] is True
    assert attempt["tool_dispatched"] is None
    assert "thinking" not in json.dumps(attempt)


def test_rejected_final_content_is_bounded_sanitized_and_never_mutates_controller_state():
    secret = "api_key=synthetic-secret-never-persist"
    content = "not-json " + secret
    adapter, _transport, task = _adapter({"provider_completion_schema_version": "provider-completion-v1", "directive_content": content, "usage": {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9}})
    snapshot = _snapshot(task, ControllerBudgetLimits.from_task_constraints(task.constraints))
    with pytest.raises(LiveModelAdapterError):
        adapter.next_directive(snapshot)
    assert adapter.metrics.model_responses == 1
    assert adapter.directive_attempts[0]["provider_transport_completed"] is True
    assert adapter.directive_attempts[0]["directive_accepted"] is False
    assert adapter.directive_attempts[0]["tool_dispatched"] is False
    evidence = adapter.directive_rejection_evidence[0]
    assert validate_rejection_evidence(evidence)
    assert evidence["raw_hash_withheld"] is True
    assert evidence["content_sha256"] is None
    assert secret not in json.dumps(evidence)
    assert evidence["evidence_sufficiency"] == "insufficient"
    assert snapshot.budget_state.patch_attempts == 0


def test_direct_mapping_and_provider_content_have_identical_acceptance():
    directive = {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}
    direct, _transport, direct_task = _adapter({"directive": directive})
    content, _transport2, content_task = _adapter({"provider_completion_schema_version": "provider-completion-v1", "directive_content": json.dumps(directive)})
    left = direct.next_directive(_snapshot(direct_task, ControllerBudgetLimits.from_task_constraints(direct_task.constraints)))
    right = content.next_directive(_snapshot(content_task, ControllerBudgetLimits.from_task_constraints(content_task.constraints)))
    assert left.name is right.name
    assert left.arguments == right.arguments


@pytest.mark.parametrize("directive", [
    {"kind": "action", "name": "apply_patch", "arguments": {"patch": "x"}},
    {"kind": "transition", "target_state": "Done", "reason": "not legal here"},
    {"kind": "action", "name": "run_reproduction", "arguments": {"phase": 3}},
    {"kind": "action"},
])
def test_semantic_parser_rejections_retain_exact_provider_final_content(directive):
    content = json.dumps(directive, separators=(",", ":"))
    adapter, _transport, task = _adapter({"provider_completion_schema_version": "provider-completion-v1", "directive_content": content})
    with pytest.raises(LiveModelAdapterError):
        adapter.next_directive(_snapshot(task, ControllerBudgetLimits.from_task_constraints(task.constraints)))
    evidence = adapter.directive_rejection_evidence[0]
    assert evidence["content_sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert evidence["content_representation"]["text"] == content
    assert evidence["evidence_sufficiency"] == "sufficient"
    assert adapter.directive_attempts[0]["tool_dispatched"] is False


def test_authoritative_controller_dispatch_marks_accepted_action_true():
    class OneDirectiveTransport:
        def request(self, payload, timeout_seconds):
            return {"directive": {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}}

    parent = Path(tempfile.mkdtemp(prefix="level32-dispatch-test-"))
    try:
        result = run_live_case(
            repository_root=str(ROOT),
            task_id=TASK_ID,
            policy=DemoPolicy.STATIC_BASELINE,
            repetition=1,
            workspace_parent=str(parent),
            config=LiveModelConfig("test-model", ("test-model-command",)),
            limits=LiveRunLimits(max_model_requests=1, max_controller_steps=1, max_retries=0, continue_on_task_failure=False),
            transport=OneDirectiveTransport(),
            retain_observable_model_directives=True,
        )
    finally:
        shutil.rmtree(parent, ignore_errors=True)
    attempt = result.evidence["observable_model_directive_attempts"][0]
    assert attempt["provider_transport_completed"] is True
    assert attempt["directive_accepted"] is True
    assert attempt["tool_dispatched"] is True


def test_accepted_transition_is_explicitly_non_tool_dispatch():
    directive = {"directive": {"kind": "transition", "target_state": "Understand", "reason": "reproduced"}}
    adapter, _transport, task = _adapter(directive)
    adapter.next_directive(_snapshot(task, ControllerBudgetLimits.from_task_constraints(task.constraints)))
    adapter.reconcile_tool_dispatch(type("Result", (), {"steps": ()})())
    assert adapter.directive_attempts[0]["directive_accepted"] is True
    assert adapter.directive_attempts[0]["tool_dispatched"] is None


def test_treatment_budget_is_global_and_action_caps_cannot_bind_first():
    budget = LiveTreatmentBudget()
    assert budget.logical_decision_ceiling == 40
    assert budget.max_controller_steps == budget.max_model_requests == 40
    assert budget.max_patch_attempts == budget.max_test_runs == budget.max_pdb_observations == budget.max_source_observations == 40
    with pytest.raises(Exception, match="authoritative global envelope"):
        LiveRunLimits(max_model_requests=39, max_controller_steps=40, max_retries=0, treatment_budget=budget)


def test_controller_ceiling_is_classified_as_budget_limited():
    class OneDirectiveTransport:
        def request(self, payload, timeout_seconds):
            return {"directive": {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}}

    parent = Path(tempfile.mkdtemp(prefix="level32-budget-test-"))
    try:
        result = run_live_case(
            repository_root=str(ROOT),
            task_id=TASK_ID,
            policy=DemoPolicy.STATIC_BASELINE,
            repetition=1,
            workspace_parent=str(parent),
            config=LiveModelConfig("test-model", ("test-model-command",)),
            limits=LiveRunLimits(max_model_requests=1, max_controller_steps=1, max_retries=0, continue_on_task_failure=False),
            transport=OneDirectiveTransport(),
        )
    finally:
        shutil.rmtree(parent, ignore_errors=True)
    assert result.status is LiveCaseStatus.BUDGET_LIMITED
    assert result.controller["stop_reason"] == "model_call_limit"


def test_model_visible_resource_budget_is_truthful_for_new_treatment_without_new_guidance():
    directive = {"directive": {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}}
    visible = ControllerBudgetLimits.from_task_constraints(_task().constraints)
    internal = LiveTreatmentBudget().controller_limits()
    baseline, base_transport, base_task = _adapter(directive, visible_limits=visible)
    treated_task = _task()
    treated_projection = treated_task.agent_visible_mapping(resource_limits={
        "max_patch_attempts": internal.max_patch_attempts,
        "max_test_runs": internal.max_test_runs,
        "max_pdb_observations": internal.max_pdb_observations,
    })
    treated, treated_transport, treated_task = _adapter(directive, limits=LiveRunLimits(max_model_requests=40, max_controller_steps=40, max_retries=0, treatment_budget=LiveTreatmentBudget()), visible_limits=internal, visible_task=treated_projection)
    baseline.next_directive(_snapshot(base_task, visible))
    treated.next_directive(_snapshot(treated_task, internal))
    def digest(request):
        normalized = json.loads(json.dumps(request))
        normalized["protocol"]["request_id"] = "request-id-placeholder"
        raw = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        return hashlib.sha256(raw).hexdigest()
    assert digest(base_transport.requests[0]) != digest(treated_transport.requests[0])
    assert base_transport.requests[0]["controller"]["budget_limits"] != treated_transport.requests[0]["controller"]["budget_limits"]
    assert treated_transport.requests[0]["controller"]["budget_limits"] == {
        "max_patch_attempts": 40,
        "max_test_runs": 40,
        "max_pdb_observations": 40,
        "max_active_hypotheses": 3,
        "max_source_observations": 40,
    }
    assert base_transport.requests[0]["instructions"] == treated_transport.requests[0]["instructions"]
    assert base_transport.requests[0]["task"] != treated_transport.requests[0]["task"]
    assert base_transport.requests[0]["task"]["constraints"]["allowed_write_paths"] == treated_transport.requests[0]["task"]["constraints"]["allowed_write_paths"]
    assert base_transport.requests[0]["task"]["description"] == treated_transport.requests[0]["task"]["description"]
    assert treated_transport.requests[0]["task"]["constraints"]["max_patch_attempts"] == 40
    assert treated_transport.requests[0]["task"]["constraints"]["max_test_runs"] == 40
    assert treated_transport.requests[0]["task"]["constraints"]["max_pdb_observations"] == 40


def test_actual_level32_new_treatment_request_and_disposable_task_json_share_projection():
    row = level32_operator._load_official_row()
    with tempfile.TemporaryDirectory(prefix="level32-projection-test-") as temp:
        root = Path(temp)
        fixture = root / "agentic_debugger/datasets/curated" / level32_operator.TASK_ID
        fixture.mkdir(parents=True)
        (fixture / "tests").mkdir()
        level32_operator._write_public_scaffold(fixture, str(row["problem_statement"]))
        task = DebugTask.from_mapping(json.loads((fixture / "task.json").read_text()))
        budget = LiveTreatmentBudget()
        controller_limits = budget.controller_limits()
        resource_limits = {
            "max_patch_attempts": controller_limits.max_patch_attempts,
            "max_test_runs": controller_limits.max_test_runs,
            "max_pdb_observations": controller_limits.max_pdb_observations,
        }
        frozen_projection = task.agent_visible_mapping()
        treated_projection = task.agent_visible_mapping(resource_limits=resource_limits)
        task_json = root / "disposable-task" / "task.json"
        task_json.parent.mkdir()
        task_json.write_text(json.dumps(treated_projection, sort_keys=True, indent=2) + "\n")

        class Transport:
            def __init__(self):
                self.requests = []

            def request(self, payload, timeout_seconds):
                self.requests.append(payload)
                return {"directive": {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}}

        transport = Transport()
        adapter = LiveModelAdapter(
            task=task,
            policy=DemoPolicy.STATIC_BASELINE,
            config=LiveModelConfig("test-model", ("test-model-command",)),
            transport=transport,
            limits=LiveRunLimits(max_model_requests=40, max_controller_steps=40, max_retries=0, treatment_budget=budget),
            registry=_registry(),
            model_visible_budget_limits=controller_limits,
            model_visible_task=treated_projection,
        )
        adapter.next_directive(_snapshot(task, controller_limits))
        request = transport.requests[0]
        request_task = request["task"]
        disposable_task = json.loads(task_json.read_text())

        def values_for_key(value, key):
            values = []
            if isinstance(value, dict):
                for name, item in value.items():
                    if name == key:
                        values.append(item)
                    values.extend(values_for_key(item, key))
            elif isinstance(value, list):
                for item in value:
                    values.extend(values_for_key(item, key))
            return values

        for visible in (request_task, disposable_task):
            assert values_for_key(visible, "max_patch_attempts") == [40]
            assert values_for_key(visible, "max_test_runs") == [40]
            assert values_for_key(visible, "max_pdb_observations") == [40]
            assert 2 not in values_for_key(visible, "max_patch_attempts")
            assert 5 not in values_for_key(visible, "max_test_runs")
            assert 6 not in values_for_key(visible, "max_pdb_observations")
        for field in resource_limits:
            assert request_task["constraints"][field] == request["controller"]["budget_limits"][field]
        assert request_task == disposable_task
        assert request_task["constraints"]["max_patch_attempts"] == 40
        assert request_task["constraints"]["max_test_runs"] == 40
        assert request_task["constraints"]["max_pdb_observations"] == 40
        assert request_task["constraints"]["allowed_write_paths"] == frozen_projection["constraints"]["allowed_write_paths"]
        assert request_task["constraints"]["denied_write_paths"] == frozen_projection["constraints"]["denied_write_paths"]
        assert request_task["tests"] == frozen_projection["tests"]
        assert request_task["description"] == frozen_projection["description"]
        semantic_request = json.loads(json.dumps(request_task))
        semantic_frozen = json.loads(json.dumps(frozen_projection))
        for mapping in (semantic_request, semantic_frozen):
            for field in resource_limits:
                mapping["constraints"].pop(field)
        assert semantic_request == semantic_frozen


def test_legacy_task_projection_remains_historical_without_treatment_overlay():
    task = _task()
    assert task.agent_visible_mapping() == task.agent_visible_mapping(resource_limits=None)
    assert task.agent_visible_mapping()["constraints"]["max_patch_attempts"] == task.constraints.max_patch_attempts
    assert task.agent_visible_mapping()["constraints"]["max_test_runs"] == task.constraints.max_test_runs
    assert task.agent_visible_mapping()["constraints"]["max_pdb_observations"] == task.constraints.max_pdb_observations


def test_frozen_model_visible_protocol_hash_and_version_remain_unchanged():
    assert cloud_adapter.PROTOCOL_VERSION == "1.3"
    assert hashlib.sha256(cloud_adapter.SYSTEM_PROMPT.encode("utf-8")).hexdigest() == "56c800314fafe18676e477877ea8cff13bafbf8ce791b71713734672d5ef7709"


def test_actual_cookiecutter_level32_task_semantics_match_accepted_frozen_evidence():
    row = level32_operator._load_official_row()
    assert level32_operator.TASK_ID == "swr-audreyr-cookiecutter-967-pdb"
    assert level32_operator.INSTANCE_ID == "audreyr__cookiecutter-967"
    assert row["repo"] == "audreyr/cookiecutter"
    assert row["base_commit"] == level32_operator.BASE_COMMIT
    assert hashlib.sha256(row["problem_statement"].encode("utf-8")).hexdigest() == "b3381a6f3f5cb16c849514751cc7cb11da3d40c5bad7d83a67cb788c2fb07047"
    assert level32_operator.SOURCE_SHA256 == "71de7ea915fee31e4e9104b89259deaa1c83ae0c8d3cbe249c878f5adbd5f6ee"
    assert level32_operator.PUBLIC_F2P == "tests/test_pdb_public_config_merge.py::test_builtin_abbreviations_survive_custom_config"
    assert level32_operator.PUBLIC_P2P == "tests/test_pdb_public_config_merge.py::test_scalar_override_preserves_other_defaults"
    assert row["FAIL_TO_PASS"] == [
        "tests/test_get_config.py::test_merge_configs",
        "tests/test_get_config.py::test_get_config",
        "tests/test_get_user_config.py::test_get_user_config_valid",
        "tests/test_get_user_config.py::test_specify_config_path",
        "tests/test_get_user_config.py::test_default_config_from_env_variable",
    ]
    assert row["PASS_TO_PASS"] == [
        "tests/test_get_config.py::test_get_config_does_not_exist",
        "tests/test_get_config.py::test_invalid_config",
        "tests/test_get_config.py::test_get_config_with_defaults",
        "tests/test_get_user_config.py::test_get_user_config_invalid",
        "tests/test_get_user_config.py::test_get_user_config_nonexistent",
        "tests/test_get_user_config.py::test_default_config_path",
        "tests/test_get_user_config.py::test_force_default_config",
        "tests/test_get_user_config.py::test_expand_user_for_directories_in_config",
        "tests/test_get_user_config.py::test_expand_vars_for_directories_in_config",
    ]
    scenario = level32_operator._scenario()
    assert scenario.localization.file_path == "cookiecutter/config.py"
    assert scenario.localization.symbol == "get_config"
    assert scenario.runtime_probe.module_path == "cookiecutter/config.py"
    assert scenario.runtime_probe.focus_function == "get_config"
    assert scenario.runtime_probe.anchor == "config_dict.update(yaml_dict)"
    assert scenario.runtime_probe.inspect_expressions == ("yaml_dict", "config_dict")
    assert scenario.runtime_probe.breakpoint_line == 54
    assert scenario.runtime_probe.exact_public_reproduction is True
    historical = (ROOT / "experiments/pdb_capability_ladder/level32-cookiecutter-967-gpt-oss-v3")
    contract = (historical / "contract.md").read_text(encoding="utf-8")
    result = json.loads((historical / "result.json").read_text(encoding="utf-8"))
    evidence = (historical / "live-results.json").read_text(encoding="utf-8")
    assert "audreyr__cookiecutter-967" in contract
    assert result["task"]["task_id"] == level32_operator.TASK_ID
    assert result["task"]["instance_id"] == level32_operator.INSTANCE_ID
    assert result["task"]["base_commit"] == level32_operator.BASE_COMMIT
    assert result["official_verifier"]["evaluator_commit"] == level32_operator.EVALUATOR_COMMIT
    assert result["official_verifier"]["image_id"] == level32_operator.IMAGE_ID
    assert '"breakpoint_line": 64' in evidence
    assert "yaml_dict" in evidence and "config_dict" in evidence
    assert "Docker" in contract and "hidden" in contract
    audit = (ROOT / "_ai-review/level32-harness-budget-observability-repair-v1/frozen-contract-audit.json").read_text(encoding="utf-8")
    assert "swr-audreyr-cookiecutter-967-pdb" in audit
    assert "curated-none-handling-001" not in audit


def test_frozen_level32_ladder_manifest_matches_pre_campaign_evidence_set():
    manifest = ROOT / "checkpoints/level32-model-escalation-frozen-ladder-sha256-2026-08-22.txt"
    expected = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and len(parts[0]) == 64:
            expected[parts[1].replace("/", "\\")] = parts[0]
    actual = {}
    evidence_root = ROOT / "experiments/pdb_capability_ladder"
    for path in evidence_root.rglob("*"):
        if path.is_file():
            actual[str(path.relative_to(evidence_root)).replace("/", "\\")] = hashlib.sha256(path.read_bytes()).hexdigest()
    mismatches = sorted(path for path in expected.keys() & actual.keys() if expected[path].lower() != actual[path].lower())
    additions = sorted(actual.keys() - expected.keys())
    deletions = sorted(expected.keys() - actual.keys())
    assert len(expected) == 36
    assert sorted(expected.keys()) == sorted(set(expected.keys()) & set(actual.keys()))
    assert mismatches == []
    assert deletions == []
    assert isinstance(additions, list)


def test_canonical_kimi_v6_counterfactual_uses_unique_successful_proof_roles():
    artifact = ROOT / "experiments/pdb_capability_ladder/level32-cookiecutter-967-kimi-k2.6-cloud-v6/live-results.json"
    raw = artifact.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "c82cd4e09c5b3b7b5a3a8801b0143872fa5bc7a421ab068b5102ffc1d0e5fead"
    case = json.loads(raw)
    trajectory = replay_events(case["events_jsonl"])
    observations = [
        Observation.from_mapping(event.payload["observation"])
        for event in trajectory.events
        if event.event_type.value == "observation"
    ]
    by_id = {item.observation_id: item for item in observations}

    assert by_id["observation-000000002"].name == "get_source_window"
    assert by_id["observation-000000009"].name == "stop_pdb_session"
    assert by_id["observation-000000012"].status is ObservationStatus.REJECTED
    assert by_id["observation-000000013"].status is ObservationStatus.OK

    legacy_role_counts = {
        name: sum(item.name == name for item in observations)
        for name in (
            "run_reproduction",
            "start_pdb_session",
            "get_stack_summary",
            "get_frame_locals",
            "next_pdb_session",
            "express_root_cause_hypothesis",
        )
    }
    assert legacy_role_counts["express_root_cause_hypothesis"] == 2
    legacy_reason = (
        "proof chain has missing or duplicate required observations"
        if any(count != 1 for count in legacy_role_counts.values())
        else None
    )
    assert legacy_reason == "proof chain has missing or duplicate required observations"

    allowed, reason = validate_pdb_patch_evidence(observations)
    assert allowed is True, reason
    assert reason == "exact-runtime PDB evidence and diagnosis are complete"

    expected_ids = {
        "observation-000000000",
        "observation-000000005",
        "observation-000000006",
        "observation-000000007",
        "observation-000000008",
        "observation-000000013",
    }
    role_names = {
        "run_reproduction",
        "start_pdb_session",
        "get_stack_summary",
        "get_frame_locals",
        "next_pdb_session",
        "express_root_cause_hypothesis",
    }
    successful_roles = {
        item.observation_id
        for item in observations
        if item.status is ObservationStatus.OK and item.name in role_names
    }
    assert successful_roles == expected_ids
    assert by_id["observation-000000012"] in observations

    start_contract = by_id["observation-000000005"].payload["proof"]
    diagnosis = by_id["observation-000000013"]
    locals_observation = by_id["observation-000000007"]
    assert all(
        by_id[item].payload["proof"] == start_contract
        for item in (
            "observation-000000005",
            "observation-000000006",
            "observation-000000007",
            "observation-000000008",
        )
    )
    assert diagnosis.payload["proof_contract"] == start_contract
    assert set(diagnosis.payload["evidence_refs"]) == {
        "observation-000000005",
        "observation-000000006",
        "observation-000000007",
        "observation-000000008",
    }
    local_values = {
        item["name"]: item["value"]
        for item in locals_observation.payload["locals"]
    }
    assert diagnosis.payload["observed_values"]["yaml_dict"] == local_values["yaml_dict"]
    assert is_transition_allowed(ControllerState.UNDERSTAND, ControllerState.PATCH)


def test_observability_serializer_fails_closed_for_missing_content_and_utf8_truncates():
    evidence = serialize_rejection_evidence(stage="json_failure", category="malformed_directive", reason_code="invalid_json", reason="x", content="é" * 5000)
    assert validate_rejection_evidence(evidence)
    assert evidence["evidence_sufficiency"] == "insufficient"
    missing = serialize_rejection_evidence(stage="extraction_failure", category="malformed_directive", reason_code="content_not_text", reason="x", content=None)
    assert validate_rejection_evidence(missing)
    assert missing["content_sha256"] is None
    assert missing["evidence_sufficiency"] == "insufficient"


def _canonical_kimi_v4_rejection_evidence():
    path = ROOT / "experiments/pdb_capability_ladder/level32-cookiecutter-967-kimi-k2.6-cloud-v4/live-results.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["evidence"]["observable_model_rejection_evidence"][0]


def _canonical_gemma_v2_rejection_evidence():
    path = ROOT / "experiments/pdb_capability_ladder/level32-cookiecutter-967-gemma4-31b-cloud-v2/live-results.json"
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (
        "48ef927e50d5677fe51e589731cdb3136615357b4c95572c8d7b8b994754175c"
    )
    payload = json.loads(raw)
    return payload["evidence"]["observable_model_rejection_evidence"][0]


def _canonical_mistral_v1_rejection_evidence():
    path = ROOT / "experiments/pdb_capability_ladder/level32-cookiecutter-967-mistral-large-3-675b-cloud-v1/live-results.json"
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (
        "70df5fcf5c74509407d10746f8051687dcbb70f4dfbfdb587ca925f7e2eac7cb"
    )
    payload = json.loads(raw)
    return payload["evidence"]["observable_model_rejection_evidence"][0]


def _canonical_gemma_v2_events():
    path = ROOT / "experiments/pdb_capability_ladder/level32-cookiecutter-967-gemma4-31b-cloud-v2/live-results.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [json.loads(line) for line in payload["events_jsonl"].splitlines() if line.strip()]


def _canonical_gemma_v2_observations():
    return [
        Observation.from_mapping(event["payload"]["observation"])
        for event in _canonical_gemma_v2_events()
        if event["event_type"] == "observation"
    ]


def _canonical_kimi_v4_events():
    path = ROOT / "experiments/pdb_capability_ladder/level32-cookiecutter-967-kimi-k2.6-cloud-v4/live-results.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [json.loads(line) for line in payload["events_jsonl"].splitlines() if line.strip()]


def _canonical_kimi_v4_observations():
    return [
        Observation.from_mapping(event["payload"]["observation"])
        for event in _canonical_kimi_v4_events()
        if event["event_type"] == "observation"
    ]


def _canonical_nemotron_nano_v1_observations():
    path = ROOT / "experiments/pdb_capability_ladder/level32-cookiecutter-967-nemotron-3-nano-30b-cloud-v1/live-results.json"
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (
        "44dc581f0ec90fd87ad3aafd85b5e2e69bbe005a6a1f52f430034d65b60dd1f4"
    )
    payload = json.loads(raw)
    trajectory = replay_events(payload["events_jsonl"])
    return [
        Observation.from_mapping(event.payload["observation"])
        for event in trajectory.events
        if event.event_type.value == "observation"
    ]


def _historical_v4_task():
    """Build only the public task projection needed by the canonical call-12 state."""
    mapping = json.loads(json.dumps(_task().to_mapping()))
    mapping["task_id"] = "swr-audreyr-cookiecutter-967-pdb"
    mapping["fixture_path"] = "agentic_debugger/datasets/curated/swr-audreyr-cookiecutter-967-pdb"
    mapping["constraints"]["allowed_write_paths"] = ["cookiecutter/config.py"]
    mapping["constraints"]["denied_write_paths"] = ["tests", "task.json"]
    mapping["reproduction"] = {
        "argv": [
            "python", "-m", "pytest",
            "tests/test_pdb_public_config_merge.py::test_builtin_abbreviations_survive_custom_config",
            "-q", "-p", "no:cacheprovider", "-o", "addopts=",
        ],
        "cwd": ".",
        "timeout_seconds": 20,
        "expected_exit_code": 1,
    }
    mapping["tests"]["fail_to_pass"] = [
        "tests/test_pdb_public_config_merge.py::test_builtin_abbreviations_survive_custom_config"
    ]
    mapping["tests"]["pass_to_pass"] = [
        "tests/test_pdb_public_config_merge.py::test_scalar_override_preserves_other_defaults"
    ]
    return DebugTask.from_mapping(mapping)


def _historical_v4_adapter():
    evidence = _canonical_kimi_v4_rejection_evidence()
    content = evidence["content_representation"]["text"]
    observations = _canonical_kimi_v4_observations()
    by_name = {item.name: item for item in observations}
    baseline = by_name["run_reproduction"]
    proof_observations = [
        baseline,
        by_name["start_pdb_session"],
        by_name["get_stack_summary"],
        by_name["get_frame_locals"],
        by_name["next_pdb_session"],
    ]
    revise = json.loads(
        json.dumps(
            json.loads(
                (ROOT / "experiments/pdb_capability_ladder/level32-cookiecutter-967-kimi-k2.6-cloud-v4/live-results.json").read_text(encoding="utf-8")
            )["evidence"]["observable_model_directives"][11]["directive"]
        )
    )
    hypothesis = RootCauseHypothesis(
        hypothesis_id=revise["hypothesis_id"],
        statement=revise["statement"],
        confidence=HypothesisConfidence(revise["confidence"]),
        status=HypothesisStatus.ACTIVE,
        evidence_refs=tuple(revise["evidence_refs"]),
        requires_runtime_evidence=revise["requires_runtime_evidence"],
        revision=2,
    )
    task = _historical_v4_task()
    limits = LiveTreatmentBudget().controller_limits()

    class Transport:
        def __init__(self):
            self.responses = [
                {
                    "provider_completion_schema_version": "provider-completion-v1",
                    "directive_content": content,
                },
                {"directive": {"kind": "transition", "target_state": "Patch", "reason": "diagnosis evidence complete"}},
            ]

        def request(self, payload, timeout_seconds):
            return self.responses.pop(0)

    proof_contract = by_name["start_pdb_session"].payload["proof"]

    def validate(arguments):
        return dict(arguments)

    def diagnose(action, arguments):
        return ToolResult(
            ObservationStatus.OK,
            {
                **arguments,
                "proof_contract": proof_contract,
            },
            "root-cause hypothesis recorded",
        )

    registry = ToolRegistry((ToolSpec(
        ActionName.EXPRESS_ROOT_CAUSE_HYPOTHESIS,
        validate,
        diagnose,
        argument_contract={
            "required": [
                "hypothesis_id", "statement", "target_file", "target_symbol",
                "confidence", "evidence_refs", "observed_values",
            ],
            "properties": {
                "hypothesis_id": {"type": "string"},
                "statement": {"type": "string"},
                "target_file": {"type": "string"},
                "target_symbol": {"type": "string"},
                "confidence": {"type": "string"},
                "evidence_refs": {"type": "array"},
                "observed_values": {"type": "object"},
            },
            "additional_properties": False,
        },
    ),))
    adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        config=LiveModelConfig("test-model", ("test-model-command",)),
        transport=Transport(),
        limits=LiveRunLimits(max_model_requests=2, max_controller_steps=2, max_retries=0),
        registry=registry,
        proof_required=True,
        proof_source_line=64,
        proof_observed_local_names=("yaml_dict", "config_dict"),
        model_visible_budget_limits=limits,
        model_visible_task=task.agent_visible_mapping(resource_limits={
            "max_patch_attempts": limits.max_patch_attempts,
            "max_test_runs": limits.max_test_runs,
            "max_pdb_observations": limits.max_pdb_observations,
        }),
        run_id=baseline.run_id,
        case_id="historical-v4-counterfactual",
        trajectory_id="historical-v4-counterfactual",
    )
    adapter._failure_reproduced = True
    adapter._proof_observations = list(proof_observations)
    snapshot = ControllerSnapshot(
        baseline.run_id,
        baseline.task_id,
        ControllerState.UNDERSTAND,
        12,
        limits,
        ControllerBudgetState(test_runs=1, pdb_observations=3, source_observations=1),
        HypothesisLedger((hypothesis,)),
    )
    return adapter, snapshot, proof_observations, proof_contract


def test_nemotron_nano_v1_exited_control_cannot_unlock_diagnosis_or_patch():
    observations = _canonical_nemotron_nano_v1_observations()
    by_id = {item.observation_id: item for item in observations}
    selected = [
        by_id["observation-000000000"],
        by_id["observation-000000006"],
        by_id["observation-000000007"],
        by_id["observation-000000008"],
        by_id["observation-000000009"],
    ]
    allowed, reason = validate_pdb_runtime_evidence(selected)
    assert allowed is False
    assert reason == "step/next did not produce a paused production frame"

    locals_values = {
        item["name"]: item.get("value")
        for item in by_id["observation-000000008"].payload["locals"]
    }
    diagnosis = Observation(
        observation_id="observation-counterfactual-diagnosis",
        action_id="action-counterfactual-diagnosis",
        run_id=selected[0].run_id,
        task_id=selected[0].task_id,
        name=ActionName.EXPRESS_ROOT_CAUSE_HYPOTHESIS.value,
        status=ObservationStatus.OK,
        payload={
            "evidence_refs": [item.observation_id for item in selected[1:]],
            "observed_values": {"yaml_dict": locals_values["yaml_dict"]},
            "proof_contract": selected[1].payload["proof"],
        },
        summary="provider-free V1 counterfactual diagnosis",
        truncated=False,
    )
    allowed, reason = validate_pdb_patch_evidence((*selected, diagnosis))
    assert allowed is False
    assert reason == "step/next did not produce a paused production frame"


def test_exited_control_resets_selected_runtime_cycle_and_requires_fresh_start():
    observations = _canonical_nemotron_nano_v1_observations()
    by_id = {item.observation_id: item for item in observations}
    adapter, snapshot, _proof_observations, _proof_contract = _historical_v4_adapter()
    adapter._proof_observations = [
        by_id["observation-000000000"],
        by_id["observation-000000006"],
        by_id["observation-000000007"],
        by_id["observation-000000008"],
    ]
    adapter.proof_cycle_events = []

    adapter._observe_snapshot(replace(
        snapshot,
        run_id=by_id["observation-000000009"].run_id,
        task_id=by_id["observation-000000009"].task_id,
        state=ControllerState.RUNTIME_EVIDENCE,
        last_observation=by_id["observation-000000009"],
    ))

    assert [item.observation_id for item in adapter._proof_observations] == [
        "observation-000000000"
    ]
    assert adapter._proof_diagnosis_ready() is False
    assert adapter._proof_runtime_progress() == {
        "next_required_actions": ["start_pdb_session"],
        "pre_diagnosis_ready": False,
        "session_active": False,
    }
    assert adapter.proof_cycle_events == [{
        "schema_version": "pdb-proof-cycle-event-v1",
        "event": "selected_runtime_cycle_reset",
        "trigger_observation_id": "observation-000000009",
        "trigger_action": "next_pdb_session",
        "trigger_state": "exited",
        "reason": "control did not pause in the declared production frame and the session is inactive",
        "removed_selected_observation_ids": [
            "observation-000000006",
            "observation-000000007",
            "observation-000000008",
        ],
        "trigger_retained_in_trajectory": True,
    }]

    # A repeated adapter read of the same controller snapshot is idempotent.
    adapter._observe_snapshot(replace(
        snapshot,
        run_id=by_id["observation-000000009"].run_id,
        task_id=by_id["observation-000000009"].task_id,
        state=ControllerState.RUNTIME_EVIDENCE,
        last_observation=by_id["observation-000000009"],
    ))
    assert len(adapter.proof_cycle_events) == 1


def test_exact_kimi_v4_content_recovers_only_the_redundant_final_brace():
    evidence = _canonical_kimi_v4_rejection_evidence()
    content = evidence["content_representation"]["text"]
    with pytest.raises(json.JSONDecodeError):
        json.loads(content)
    value, raw_content, provenance = _resolve_provider_directive({
        "provider_completion_schema_version": "provider-completion-v1",
        "directive_content": content,
    })
    assert isinstance(value, dict)
    assert value["kind"] == "action"
    assert value["name"] == "express_root_cause_hypothesis"
    assert raw_content == content
    assert provenance["directive_transport_normalized"] is True
    assert provenance["normalization_schema_version"] == DIRECTIVE_NORMALIZATION_SCHEMA_VERSION
    assert provenance["normalization_policy_id"] == DIRECTIVE_NORMALIZATION_POLICY_ID
    assert provenance["normalization_before"]["byte_length"] == len(content.encode("utf-8"))
    assert provenance["normalization_after"]["byte_length"] == len(content[:-1].encode("utf-8"))
    assert provenance["normalization_removed_suffix"]["text"] == "}"


def test_exact_kimi_v4_content_reaches_canonical_semantic_parser_after_recovery():
    evidence = _canonical_kimi_v4_rejection_evidence()
    content = evidence["content_representation"]["text"]
    adapter, snapshot, proof_observations, proof_contract = _historical_v4_adapter()

    contracts = adapter._effective_contract(snapshot)
    assert set(contracts) == {"express_root_cause_hypothesis"}
    assert "action" in adapter._effective_directive_schema(snapshot)
    value, raw_content, normalization = _resolve_provider_directive({
        "provider_completion_schema_version": "provider-completion-v1",
        "directive_content": content,
    })
    assert raw_content == content
    assert normalization["directive_transport_normalized"] is True
    parsed = _parse(
        value,
        snapshot,
        action_contracts=contracts,
        directive_kinds={"action"},
        directive_schema=adapter._effective_directive_schema(snapshot),
        legal_transition_targets=set(),
    )
    assert isinstance(parsed, ActionDirective)
    assert parsed.name is ActionName.EXPRESS_ROOT_CAUSE_HYPOTHESIS
    required = contracts["express_root_cause_hypothesis"]["required"]
    assert set(required) <= set(parsed.arguments)
    assert parsed.arguments["hypothesis_id"] == "hypothesis-1"
    assert parsed.arguments["target_file"] == "cookiecutter/config.py"
    assert parsed.arguments["target_symbol"] == "get_config"
    assert parsed.arguments["evidence_refs"] == [
        "observation-000000005", "observation-000000006",
        "observation-000000007", "observation-000000008",
    ]

    accepted = adapter.next_directive(snapshot)
    assert isinstance(accepted, ActionDirective)
    assert adapter.directive_attempts[0]["directive_transport_normalized"] is True
    assert adapter.metrics.retries == 0

    diagnosis = adapter.registry.dispatch(
        Action(
            action_id="action-counterfactual-diagnosis",
            run_id=snapshot.run_id,
            task_id=snapshot.task_id,
            state=snapshot.state,
            name=accepted.name.value,
            arguments=dict(accepted.arguments),
        ),
        observation_id="observation-counterfactual-diagnosis",
    )
    assert diagnosis.status is ObservationStatus.OK
    assert diagnosis.payload["evidence_refs"] == parsed.arguments["evidence_refs"]
    assert diagnosis.payload["observed_values"] == parsed.arguments["observed_values"]
    assert diagnosis.payload["proof_contract"] == proof_contract
    assert validate_pdb_patch_evidence((*proof_observations, diagnosis))[0]
    adapter._proof_observations.append(diagnosis)
    assert adapter._proof_diagnosis_ready() is True
    assert adapter._proof_patch_allowed() is True

    transition_snapshot = replace(
        snapshot,
        model_call_index=13,
        last_observation=diagnosis,
    )
    transition = adapter.next_directive(transition_snapshot)
    assert isinstance(transition, TransitionDirective)
    assert transition.target_state is ControllerState.PATCH


def test_exact_gemma_v2_fence_recovers_one_strict_mapping_with_exact_provenance():
    evidence = _canonical_gemma_v2_rejection_evidence()
    content = evidence["content_representation"]["text"]
    assert len(content.encode("utf-8")) == 271
    assert hashlib.sha256(content.encode("utf-8")).hexdigest() == (
        "6cc50ea0fd91f50abd004f2483cfc6cdd03e5da604f0660adfb6b40d6d4b581c"
    )
    with pytest.raises(json.JSONDecodeError):
        json.loads(content)

    value, raw_content, provenance = _resolve_provider_directive({
        "provider_completion_schema_version": "provider-completion-v1",
        "directive_content": content,
    })
    inner = content[len("```json\n"):-len("\n```")]
    assert raw_content == content
    assert value == json.loads(inner)
    assert value["kind"] == "revise_hypothesis"
    assert provenance["directive_transport_normalized"] is True
    assert provenance["normalization_schema_version"] == DIRECTIVE_NORMALIZATION_SCHEMA_VERSION
    assert provenance["normalization_policy_id"] == DIRECTIVE_NORMALIZATION_POLICY_ID
    assert provenance["normalization_kind"] == "exact_json_markdown_fence"
    assert provenance["normalization_after"] == {
        "byte_length": len(inner.encode("utf-8")),
        "sha256": hashlib.sha256(inner.encode("utf-8")).hexdigest(),
        "raw_hash_withheld": False,
    }
    assert provenance["normalization_removed_prefix"]["text"] == "```json\n"
    assert provenance["normalization_removed_suffix"]["text"] == "\n```"


def test_exact_mistral_v1_fence_then_brace_recovers_only_the_ordered_composition():
    evidence = _canonical_mistral_v1_rejection_evidence()
    content = evidence["content_representation"]["text"]
    assert hashlib.sha256(content.encode("utf-8")).hexdigest() == (
        "42d3cce67157ebebb0467fe8e29784fe63753ec93df46465a6bc4fdb78f1e540"
    )
    with pytest.raises(json.JSONDecodeError):
        json.loads(content)

    value, raw_content, provenance = _resolve_provider_directive({
        "provider_completion_schema_version": "provider-completion-v1",
        "directive_content": content,
    })
    inner = content[len("```json\n"):-len("\n```")]
    final = inner[:-len("\n}")]
    assert raw_content == content
    assert value == json.loads(final)
    assert value["kind"] == "revise_hypothesis"
    assert provenance["normalization_schema_version"] == DIRECTIVE_NORMALIZATION_SCHEMA_VERSION
    assert provenance["normalization_policy_id"] == DIRECTIVE_NORMALIZATION_POLICY_ID
    assert provenance["normalization_kind"] == (
        "exact_json_markdown_fence_then_redundant_trailing_closing_delimiter"
    )
    assert provenance["normalization_before"]["sha256"] == evidence["content_sha256"]
    assert provenance["normalization_after"]["sha256"] == hashlib.sha256(final.encode("utf-8")).hexdigest()
    assert provenance["normalization_removed_prefix"]["text"] == "```json\n"
    assert provenance["normalization_removed_suffix"]["text"] == "\n}\n```"
    assert [step["kind"] for step in provenance["normalization_steps"]] == [
        "exact_json_markdown_fence",
        "redundant_trailing_closing_delimiter",
    ]
    assert provenance["normalization_steps"][0]["after"]["sha256"] == hashlib.sha256(inner.encode("utf-8")).hexdigest()
    assert provenance["normalization_steps"][1]["before"] == provenance["normalization_steps"][0]["after"]
    assert provenance["normalization_steps"][1]["after"] == provenance["normalization_after"]


def test_exact_mistral_v1_composed_mapping_enters_canonical_semantic_parser():
    content = _canonical_mistral_v1_rejection_evidence()["content_representation"]["text"]
    value, _raw_content, provenance = _resolve_provider_directive({
        "provider_completion_schema_version": "provider-completion-v1",
        "directive_content": content,
    })
    artifact = ROOT / "experiments/pdb_capability_ladder/level32-cookiecutter-967-mistral-large-3-675b-cloud-v1/live-results.json"
    canonical = json.loads(artifact.read_text(encoding="utf-8"))
    add = next(
        item["directive"]
        for item in canonical["evidence"]["observable_model_directives"]
        if item["directive"]["kind"] == "add_hypothesis"
    )
    hypothesis = RootCauseHypothesis(
        hypothesis_id=add["hypothesis_id"],
        statement=add["statement"],
        confidence=HypothesisConfidence(add["confidence"]),
        status=HypothesisStatus.ACTIVE,
        evidence_refs=tuple(add["evidence_refs"]),
        requires_runtime_evidence=add["requires_runtime_evidence"],
        revision=1,
    )
    events = [json.loads(line) for line in canonical["events_jsonl"].splitlines() if line.strip()]
    failed_decision = next(event for event in events if event["sequence"] == 28)
    limits = LiveTreatmentBudget().controller_limits()
    snapshot = ControllerSnapshot(
        failed_decision["run_id"],
        failed_decision["task_id"],
        ControllerState.UNDERSTAND,
        11,
        limits,
        ControllerBudgetState(**failed_decision["payload"]["budget_before"]),
        HypothesisLedger((hypothesis,)),
    )
    parsed = _parse(
        value,
        snapshot,
        directive_kinds={"revise_hypothesis"},
    )
    assert isinstance(parsed, ReviseHypothesisDirective)
    assert parsed.hypothesis_id == "hypothesis-1"
    assert parsed.requires_runtime_evidence is False
    assert provenance["directive_transport_normalized"] is True


def test_exact_json_fence_allows_only_declared_outer_json_whitespace():
    inner = '{"kind":"transition","target_state":"Done","reason":"x"}'
    content = " \t\r\n```json\n" + inner + "\n```\r\n\t "
    value, raw_content, provenance = _resolve_provider_directive({
        "provider_completion_schema_version": "provider-completion-v1",
        "directive_content": content,
    })
    assert value["target_state"] == "Done"
    assert raw_content == content
    assert provenance["normalization_removed_prefix"]["text"] == " \t\r\n```json\n"
    assert provenance["normalization_removed_suffix"]["text"] == "\n```\r\n\t "


def test_exact_json_fence_accepts_strict_pretty_json_without_reserializing():
    inner = '{\n  "kind": "transition",\n  "target_state": "Done",\n  "reason": "x"\n}'
    content = "```json\n" + inner + "\n```"
    value, _raw_content, provenance = _resolve_provider_directive({
        "provider_completion_schema_version": "provider-completion-v1",
        "directive_content": content,
    })
    assert value["target_state"] == "Done"
    assert provenance["normalization_after"]["byte_length"] == len(inner.encode("utf-8"))
    assert provenance["normalization_after"]["sha256"] == hashlib.sha256(inner.encode("utf-8")).hexdigest()


def test_prose_wrapped_json_fence_recovers_only_the_single_inner_mapping():
    inner = '{"kind":"transition","target_state":"Done","reason":"x"}'
    content = "I will correct the patch first.\n```json\n" + inner + "\n```\nDone."
    value, raw_content, provenance = _resolve_provider_directive({
        "provider_completion_schema_version": "provider-completion-v1",
        "directive_content": content,
    })
    assert raw_content == content
    assert value["target_state"] == "Done"
    assert provenance["normalization_kind"] == "prose_wrapped_exact_json_markdown_fence"
    assert provenance["normalization_removed_prefix"]["text"].endswith("```json\n")
    assert provenance["normalization_removed_suffix"]["text"] == "\n```\nDone."


def test_prose_wrapped_json_object_recovers_only_one_braces_free_surrounding_note():
    inner = '{"kind":"transition","target_state":"Done","reason":"x"}'
    content = "I will correct the patch.\n" + inner + "\nI am done."
    value, raw_content, provenance = _resolve_provider_directive({
        "provider_completion_schema_version": "provider-completion-v1",
        "directive_content": content,
    })
    assert raw_content == content
    assert value["target_state"] == "Done"
    assert provenance["normalization_kind"] == "prose_wrapped_exact_json_object"


def test_unterminated_json_fence_recovers_only_one_strict_mapping():
    inner = '{"kind":"transition","target_state":"Done","reason":"x"}'
    content = "```json\n" + inner
    value, raw_content, provenance = _resolve_provider_directive({
        "provider_completion_schema_version": "provider-completion-v1",
        "directive_content": content,
    })
    assert raw_content == content
    assert value["target_state"] == "Done"
    assert provenance["normalization_kind"] == "unterminated_exact_json_markdown_fence"
    assert provenance["normalization_removed_prefix"]["text"] == "```json\n"
    assert provenance["normalization_removed_suffix"]["text"] == ""


@pytest.mark.parametrize(
    "content",
    [
        "```json\n{\"kind\":\"transition\",\"target_state\":\"Done\"} trailing prose",
    ],
)
def test_unterminated_json_fence_rejects_ambiguous_content(content):
    with pytest.raises(LiveModelAdapterError):
        _resolve_provider_directive({
            "provider_completion_schema_version": "provider-completion-v1",
            "directive_content": content,
        })


def test_normalization_hashes_are_withheld_when_recording_redacts_content():
    inner = '{"kind":"transition","target_state":"Done","reason":"token=synthetic-secret"}'
    _value, _raw_content, provenance = _resolve_provider_directive({
        "provider_completion_schema_version": "provider-completion-v1",
        "directive_content": "```json\n" + inner + "\n```",
    })
    assert provenance["normalization_before"]["sha256"] is None
    assert provenance["normalization_before"]["raw_hash_withheld"] is True
    assert provenance["normalization_after"]["sha256"] is None
    assert provenance["normalization_after"]["raw_hash_withheld"] is True


def test_exact_gemma_v2_fence_passes_canonical_parser_in_historical_controller_state(tmp_path):
    evidence = _canonical_gemma_v2_rejection_evidence()
    content = evidence["content_representation"]["text"]
    artifact = ROOT / "experiments/pdb_capability_ladder/level32-cookiecutter-967-gemma4-31b-cloud-v2/live-results.json"
    canonical = json.loads(artifact.read_text(encoding="utf-8"))
    events = _canonical_gemma_v2_events()
    observations = _canonical_gemma_v2_observations()
    by_name = {item.name: item for item in observations}
    baseline = by_name["run_reproduction"]
    stop = by_name["stop_pdb_session"]
    add_hypothesis = next(
        item["directive"]
        for item in canonical["evidence"]["observable_model_directives"]
        if item["model_call_index"] == 3
    )
    hypothesis = RootCauseHypothesis(
        hypothesis_id=add_hypothesis["hypothesis_id"],
        statement=add_hypothesis["statement"],
        confidence=HypothesisConfidence(add_hypothesis["confidence"]),
        status=HypothesisStatus.ACTIVE,
        evidence_refs=tuple(add_hypothesis["evidence_refs"]),
        requires_runtime_evidence=add_hypothesis["requires_runtime_evidence"],
        revision=1,
    )
    fixture = tmp_path / "agentic_debugger/datasets/curated" / level32_operator.TASK_ID
    (fixture / "tests").mkdir(parents=True)
    row = level32_operator._load_official_row()
    level32_operator._write_public_scaffold(fixture, str(row["problem_statement"]))
    task = DebugTask.from_mapping(json.loads((fixture / "task.json").read_text(encoding="utf-8")))
    limits = LiveTreatmentBudget().controller_limits()
    failed_decision = next(event for event in events if event["sequence"] == 28)
    budget = failed_decision["payload"]["budget_before"]
    return_transition = next(event for event in events if event["sequence"] == 27)
    assert return_transition["payload"]["target_state"] == ControllerState.UNDERSTAND.value

    class Transport:
        def request(self, payload, timeout_seconds):
            return {
                "provider_completion_schema_version": "provider-completion-v1",
                "directive_content": content,
            }

    adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        config=LiveModelConfig("test-model", ("test-model-command",)),
        transport=Transport(),
        limits=LiveRunLimits(max_model_requests=1, max_controller_steps=1, max_retries=0),
        registry=_registry(),
        proof_required=True,
        proof_source_line=54,
        proof_observed_local_names=("yaml_dict", "config_dict"),
        model_visible_budget_limits=limits,
        model_visible_task=task.agent_visible_mapping(resource_limits={
            "max_patch_attempts": limits.max_patch_attempts,
            "max_test_runs": limits.max_test_runs,
            "max_pdb_observations": limits.max_pdb_observations,
        }),
        run_id=baseline.run_id,
        case_id="historical-gemma-v2-counterfactual",
        trajectory_id="historical-gemma-v2-counterfactual",
    )
    adapter._failure_reproduced = True
    adapter._proof_observations = list(observations)
    snapshot = ControllerSnapshot(
        baseline.run_id,
        baseline.task_id,
        ControllerState.UNDERSTAND,
        11,
        limits,
        ControllerBudgetState(**budget),
        HypothesisLedger((hypothesis,)),
        last_observation=stop,
    )

    schema = adapter._effective_directive_schema(snapshot)
    assert set(schema) == {"revise_hypothesis"}
    assert schema["revise_hypothesis"]["constraints"]["hypothesis_id"]["enum"] == ["hypothesis-1"]
    assert schema["revise_hypothesis"]["constraints"]["requires_runtime_evidence"]["enum"] == [False]
    directive = adapter.next_directive(snapshot)
    assert isinstance(directive, ReviseHypothesisDirective)
    assert directive.hypothesis_id == "hypothesis-1"
    assert directive.evidence_refs == (
        "observation-000000005", "observation-000000006",
        "observation-000000007", "observation-000000008",
    )
    assert directive.requires_runtime_evidence is False
    assert adapter.directive_attempts[0]["directive_transport_normalized"] is True
    assert adapter.metrics.retries == 0
    assert adapter.metrics.model_requests == 1
    assert adapter.directive_attempts[0]["normalization_before"]["sha256"] == evidence["content_sha256"]


def test_strict_json_path_has_no_normalization_provenance():
    content = json.dumps({"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}})
    value, raw_content, provenance = _resolve_provider_directive({
        "provider_completion_schema_version": "provider-completion-v1",
        "directive_content": content,
    })
    assert value["name"] == "run_reproduction"
    assert raw_content == content
    assert provenance is None


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [
        ("", "}"),
        (" \t\r\n", "\n}\t"),
        ("\n", " \t}\r\n"),
    ],
)
def test_normalization_provenance_preserves_exact_removed_suffix(prefix, suffix):
    content = "{\"kind\":\"transition\",\"target_state\":\"Done\",\"reason\":\"x\"}"
    value, _raw_content, provenance = _resolve_provider_directive({
        "provider_completion_schema_version": "provider-completion-v1",
        "directive_content": prefix + content + suffix,
    })
    assert value["target_state"] == "Done"
    removed = provenance["normalization_removed_suffix"]
    assert removed["byte_length"] == len(suffix.encode("utf-8"))
    assert removed["sha256"] == hashlib.sha256(suffix.encode("utf-8")).hexdigest()
    assert removed["text"] == suffix


@pytest.mark.parametrize("content", [
    "text {\"kind\":\"transition\",\"target_state\":\"Done\",\"reason\":\"x\"}}",
    "{\"kind\":\"transition\",\"target_state\":\"Done\",\"reason\":\"x\"}} trailing text",
    "{\"kind\":\"transition\",\"target_state\":\"Done\",\"reason\":\"x\"}\n{\"kind\":\"transition\",\"target_state\":\"Failed\",\"reason\":\"x\"}",
    "{\"kind\":\"transition\",\"target_state\":\"Done\",\"reason\":\"x\"}}}",
    "{\"kind\":\"transition\",\"target_state\":\"Done\",\"reason\":\"x\"}]",
    "{\"kind\":\"transition\",\"target_state\":\"Done\",\"reason\":\"x\"",
    "[1]",
    "null",
])
def test_narrow_normalization_rejects_non_mechanical_suffixes(content):
    with pytest.raises(LiveModelAdapterError):
        _resolve_provider_directive({
            "provider_completion_schema_version": "provider-completion-v1",
            "directive_content": content,
        })


@pytest.mark.parametrize("content", [
    "```JSON\n{\"kind\":\"transition\",\"target_state\":\"Done\",\"reason\":\"x\"}\n```",
    "```\n{\"kind\":\"transition\",\"target_state\":\"Done\",\"reason\":\"x\"}\n```",
    "``` json\n{\"kind\":\"transition\",\"target_state\":\"Done\",\"reason\":\"x\"}\n```",
    "```json\r\n{\"kind\":\"transition\",\"target_state\":\"Done\",\"reason\":\"x\"}\r\n```",
    "````json\n{\"kind\":\"transition\",\"target_state\":\"Done\",\"reason\":\"x\"}\n````",
    "```json\n[{\"kind\":\"transition\",\"target_state\":\"Done\",\"reason\":\"x\"}]\n```",
    "```json\n{\"kind\":\"transition\",\"target_state\":\"Done\",\"reason\":\"x\"}\n{\"kind\":\"transition\",\"target_state\":\"Failed\",\"reason\":\"x\"}\n```",
    "```json\n{\"kind\":\"transition\",\"target_state\":\"Done\",\"reason\":\"x\"}}}\n```",
    "```json\n{\"kind\":\"transition\",\"target_state\":\"Done\",\"reason\":\"x\"}} trailing\n```",
    "```json\n{\"kind\":\"transition\",\"target_state\":\"Done\",\"reason\":\"x\"}\n```json",
])
def test_exact_json_fence_normalization_rejects_ambiguous_or_unpinned_forms(content):
    with pytest.raises(LiveModelAdapterError):
        _resolve_provider_directive({
            "provider_completion_schema_version": "provider-completion-v1",
            "directive_content": content,
        })


def test_fenced_mapping_still_enters_unchanged_canonical_semantic_parser():
    content = "```json\n{\"kind\":\"action\",\"name\":\"synthetically_illegal_action\",\"arguments\":{}}\n```"
    adapter, _transport, task = _adapter({
        "provider_completion_schema_version": "provider-completion-v1",
        "directive_content": content,
    })
    with pytest.raises(LiveModelAdapterError):
        adapter.next_directive(_snapshot(task, ControllerBudgetLimits.from_task_constraints(task.constraints)))
    attempt = adapter.directive_attempts[0]
    assert attempt["directive_transport_normalized"] is True
    assert attempt["normalization_kind"] == "exact_json_markdown_fence"
    assert attempt["directive_accepted"] is False
    assert adapter.metrics.retries == 0
    assert adapter.metrics.model_requests == 1


def test_normalized_mapping_still_enters_unchanged_canonical_semantic_parser():
    content = "{\"kind\":\"action\",\"name\":\"synthetically_illegal_action\",\"arguments\":{}}}"
    adapter, _transport, task = _adapter({
        "provider_completion_schema_version": "provider-completion-v1",
        "directive_content": content,
    })
    with pytest.raises(LiveModelAdapterError):
        adapter.next_directive(_snapshot(task, ControllerBudgetLimits.from_task_constraints(task.constraints)))
    attempt = adapter.directive_attempts[0]
    assert attempt["directive_transport_normalized"] is True
    assert attempt["normalization_removed_suffix"]["byte_length"] == 1
    assert adapter.metrics.retries == 0
    assert adapter.metrics.model_requests == 1


def test_rejection_integrity_detects_the_historical_extracted_v4_mismatch():
    canonical = _canonical_kimi_v4_rejection_evidence()
    extracted = json.loads((ROOT / "_ai-review/level32-kimi-k26-v4/directive-rejection-evidence.json").read_text(encoding="utf-8"))
    assert validate_rejection_evidence(canonical)
    assert not validate_rejection_evidence(extracted)


def test_rejection_integrity_tracks_actual_representation_and_sufficiency_separately(tmp_path):
    redacted = serialize_rejection_evidence(
        stage="json_failure",
        category="malformed_directive",
        reason_code="invalid_json",
        reason="typed parser evidence is decisive",
        content="api_key=synthetic-secret",
        classification_sufficient=True,
    )
    assert redacted["content_representation"]["redacted"] is True
    assert redacted["evidence_sufficiency"] == "sufficient"
    assert validate_rejection_evidence(redacted)
    truncated = serialize_rejection_evidence(
        stage="json_failure",
        category="malformed_directive",
        reason_code="invalid_json",
        reason="x",
        content="é" * 5000,
    )
    assert truncated["content_representation"]["truncated"] is True
    assert truncated["content_representation"]["byte_length"] == len(truncated["content_representation"]["text"].encode("utf-8"))
    assert validate_rejection_evidence(truncated)
    truncated["content_representation"]["text"] += "x"
    assert not validate_rejection_evidence(truncated)


def test_rejection_evidence_export_is_structural_and_deterministic(tmp_path):
    source = ROOT / "experiments/pdb_capability_ladder/level32-cookiecutter-967-kimi-k2.6-cloud-v4/live-results.json"
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    exported = export_rejection_evidence(source, first)
    export_rejection_evidence(source, second)
    assert json.loads(first.read_text(encoding="utf-8")) == exported
    assert first.read_bytes() == second.read_bytes()
    assert len(exported["content_representation"]["text"].encode("utf-8")) == 1369
    assert exported["content_representation"]["sha256"] == "adf09aefda5bdf0368ff7d87538aab630647ebe01e07e66a1d9d3ce23b5727ff"


def test_prepared_revisions_include_kimi_v7_and_fresh_gemma_v3():
    assert level32_operator.PREPARED_TREATMENT_REVISIONS["kimi-k2.6:cloud"] == 7
    assert level32_operator.PREPARED_TREATMENT_REVISIONS["gemma4:31b-cloud"] == 3
    assert level32_operator._treatment_id_for_model("gemma4:31b-cloud", 3) == (
        "pdb-capability-level32-cookiecutter-967-gemma4-31b-cloud-v3-"
        "workspace-derived-official-git-diff-v1"
    )
    assert level32_operator._treatment_fingerprint(
        "gemma4:31b-cloud", LiveTreatmentBudget()
    ) != "f8471e129c786978f32498072db3872a82fad5ee09338b0e1615d39167dd2378"
    assert level32_operator._treatment_id_for_model("kimi-k2.6:cloud", 7) == (
        "pdb-capability-level32-cookiecutter-967-kimi-k2.6-cloud-v7-"
        "workspace-derived-official-git-diff-v1"
    )
    assert level32_operator._treatment_id_for_model("kimi-k2.6:cloud", 6) == (
        "pdb-capability-level32-cookiecutter-967-kimi-k2.6-cloud-v6-"
        "workspace-derived-official-git-diff-v1"
    )
    assert level32_operator._treatment_id_for_model("kimi-k2.6:cloud", 5).endswith(
        "-v5-workspace-derived-official-git-diff-v1"
    )
    assert len(level32_operator._treatment_fingerprint("kimi-k2.6:cloud", LiveTreatmentBudget())) == 64


def test_kimi_operator_default_revision_resolution_uses_v7(monkeypatch, tmp_path):
    captured = {}
    original = level32_operator._treatment_id_for_model

    def capture_revision(model, revision=1):
        captured["model"] = model
        captured["revision"] = revision
        return original(model, revision)

    monkeypatch.setattr(level32_operator, "_resolve_model_or_fail", lambda model: (model, object()))
    monkeypatch.setattr(level32_operator, "_require_treatment_eligible", lambda model: None)
    monkeypatch.setattr(level32_operator, "_treatment_id_for_model", capture_revision)

    with pytest.raises(level32_operator.ProofError, match="live selection"):
        level32_operator.main(
            [
                "--model",
                "kimi-k2.6:cloud",
                "--output-dir",
                str(tmp_path / "prepared-v7"),
            ]
        )

    assert captured == {"model": "kimi-k2.6:cloud", "revision": 7}


def test_breakpoint_policy_is_versioned_and_fingerprinted(monkeypatch):
    assert PDB_BREAKPOINT_SELECTION_SCHEMA_VERSION == "pdb-breakpoint-selection-v1"
    assert PDB_BREAKPOINT_SELECTION_POLICY_ID == "model-selected-runtime-validated-v1"
    before = level32_operator._treatment_fingerprint("kimi-k2.6:cloud", LiveTreatmentBudget())
    monkeypatch.setattr(level32_operator, "PDB_BREAKPOINT_SELECTION_POLICY_ID", "test-policy-id")
    after = level32_operator._treatment_fingerprint("kimi-k2.6:cloud", LiveTreatmentBudget())
    assert after != before


def test_proof_role_selection_policy_is_versioned_and_fingerprinted(monkeypatch):
    assert PROOF_ROLE_SELECTION_SCHEMA_VERSION == "pdb-proof-role-selection-v2"
    assert PROOF_ROLE_SELECTION_POLICY_ID == "admissible-runtime-cycle-recovery-v2"
    assert PROOF_ROLE_SELECTION_POLICY["history_retained"] is True
    assert PROOF_ROLE_SELECTION_POLICY["successful_duplicates_fail_closed"] == "within_selected_runtime_cycle"
    assert PROOF_ROLE_SELECTION_POLICY["control_role_requires_paused_production_frame"] is True
    assert PROOF_ROLE_SELECTION_POLICY["exited_control_resets_selected_runtime_cycle"] is True
    before = level32_operator._treatment_fingerprint(
        "kimi-k2.6:cloud", LiveTreatmentBudget()
    )
    monkeypatch.setitem(
        PROOF_ROLE_SELECTION_POLICY,
        "policy_id",
        "test-proof-role-selection-policy",
    )
    after = level32_operator._treatment_fingerprint(
        "kimi-k2.6:cloud", LiveTreatmentBudget()
    )
    assert after != before
