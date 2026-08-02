from __future__ import annotations

import copy
import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import quixbugs_paired_pilot as pilot


@pytest.fixture
def manifest():
    return pilot.load_manifest()


def test_complete_task_inventory_is_evaluated(manifest):
    assert len(manifest["inventory"]) == 8
    assert {item["algorithm"] for item in manifest["inventory"]} == {
        "bucketsort", "find_in_sorted", "flatten", "gcd", "hanoi",
        "is_valid_parenthesization", "kheapsort", "kth",
    }


def test_gcd_and_prior_live_tasks_are_excluded(manifest):
    gcd = next(item for item in manifest["inventory"] if item["algorithm"] == "gcd")
    assert gcd["exclusion_status"] == "EXCLUDED"
    assert gcd["prior_live_use"] == "YES"
    assert "transport" in gcd["exclusion_reason"]


def test_deterministic_eligibility_ranking(manifest):
    assert manifest["selection"]["ranking"] == pilot.selection_ranking(manifest["inventory"])


def test_exactly_three_selected_tasks(manifest):
    assert manifest["selection"]["selected_count"] == 3
    assert len(manifest["selection"]["selected_task_ids"]) == 3


def test_selection_is_stable_under_input_ordering(manifest):
    reversed_inventory = list(reversed(manifest["inventory"]))
    assert pilot.selection_ranking(reversed_inventory) == manifest["selection"]["ranking"]


def test_six_unique_case_ids(manifest):
    cases = pilot.case_order(manifest["selection"]["selected_task_ids"])
    assert len(cases) == 6
    assert len({case["case_id"] for case in cases}) == 6


def test_stable_case_order(manifest):
    assert manifest["case_order"] == pilot.case_order(manifest["selection"]["selected_task_ids"])


def test_exactly_one_case_per_task_policy_pair(manifest):
    pairs = [(case["task_id"], case["policy"]) for case in manifest["case_order"]]
    assert len(pairs) == len(set(pairs)) == 6


def test_no_case_state_sharing(manifest):
    evidence = pilot.dry_run(manifest, fail_at=None)
    assert evidence["fresh_case_resources"] is True
    assert evidence["all_six_walked"] is True


def test_static_policy_cannot_open_pdb(manifest):
    case = next(case for case in manifest["case_order"] if case["policy"] == "static-baseline")
    result = pilot.public_case_record(manifest, case)
    assert result["pdb_counts"]["allowed_gate_openings"] == 0
    assert result["pdb_counts"]["sessions_started"] == 0
    assert result["pdb_counts"]["successful_observations"] == 0
    assert result["pdb_counts"]["failed_observations"] == 0


def test_pdb_policy_uses_only_the_real_gate(manifest):
    assert pilot._runtime_probe_for("quixbugs-find-in-sorted-smoke-v1").focus_function == "binsearch"
    assert manifest["fair_pair_contract"]["pdb_policy_access"].startswith("only through")


def test_identical_non_policy_budgets_across_each_pair(manifest):
    assert manifest["fair_pair_contract"]["shared"]
    assert manifest["budgets"] == pilot.FROZEN_BUDGETS


def test_manifest_hash_changes_when_frozen_field_changes(manifest):
    changed = copy.deepcopy(manifest)
    changed["budgets"]["max_hypotheses"] += 1
    assert pilot.manifest_hash(changed) != pilot.manifest_hash(manifest)


def test_manifest_rejects_post_freeze_task_substitution(manifest):
    changed = copy.deepcopy(manifest)
    changed["selection"]["selected_task_ids"][0] = "quixbugs-bucketsort-smoke-v1"
    with pytest.raises(pilot.PilotError):
        pilot.validate_manifest(changed)


def test_route_model_variant_mismatch_blocks(manifest):
    changed = copy.deepcopy(manifest)
    changed["route"]["model"] = "different/model"
    with pytest.raises(pilot.PilotError):
        pilot.validate_manifest(changed)


def test_nonzero_pricing_blocks(manifest):
    observation = {**manifest["route"], "input_price": 0.01, "output_price": 0, "paid_fallback_used": False, "alternate_provider_used": False, "ollama_used": False}
    with pytest.raises(pilot.PilotError):
        pilot.validate_route_observation(observation, manifest["route"])


def test_no_paid_or_fallback_route(manifest):
    for key in ("paid_fallback", "alternate_provider", "ollama_fallback", "model_substitution"):
        assert manifest["route"][key] is False


def test_unknown_terminal_status_rejects(manifest):
    result = pilot.public_case_record(manifest, manifest["case_order"][0], terminal_status="BOGUS")
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest)


def test_malformed_result_schema_rejects(manifest):
    result = pilot.public_case_record(manifest, manifest["case_order"][0])
    del result["candidate_hash"]
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest)


def test_no_model_qualification_reproduces_each_selected_baseline(manifest):
    selected = {item["task_id"] for item in manifest["inventory"] if item["exclusion_status"] == "SELECTED"}
    assert selected == set(pilot.EXPECTED_SELECTED)
    assert all(item["deterministic_baseline_status"] == "PASS" for item in manifest["inventory"] if item["task_id"] in selected)


def test_verifier_rejects_buggy_candidate_is_prerequisite(manifest):
    assert all(item["verifier_status"] == "PASS" for item in manifest["inventory"] if item["task_id"] in pilot.EXPECTED_SELECTED)


def test_private_correct_qualification_is_not_public(manifest):
    assert manifest["public_private_boundary"]["private_correct_candidate_in_public_records"] is False


def test_pdb_reachability_qualification_is_prerequisite(manifest):
    assert all(item["contained_pdb_reachability_status"] == "PASS" for item in manifest["inventory"] if item["task_id"] in pilot.EXPECTED_SELECTED)


def test_oracle_gold_material_is_absent_from_public_records(manifest):
    boundary = manifest["public_private_boundary"]
    assert boundary["oracle_material_in_public_records"] is False
    assert boundary["gold_patch_in_public_records"] is False


def test_canonical_source_is_restored(manifest):
    assert all(item["source_restoration_status"] == "PASS" for item in manifest["inventory"])


def test_workspace_cleanup_succeeds(manifest):
    assert all(item["source_restoration_status"] == "PASS" for item in manifest["inventory"])
    assert manifest["public_private_boundary"]["qualification_evidence_storage"].startswith("local")


def test_dry_run_starts_no_provider_process(manifest):
    assert pilot.dry_run(manifest)["provider_processes_started"] == 0


def test_dry_run_creates_no_network_activity(manifest):
    assert pilot.dry_run(manifest)["network_activity"] is False


def test_live_mode_without_explicit_authorization_blocks(manifest):
    with pytest.raises(pilot.PilotError):
        pilot.live(manifest, None)


def test_campaign_stops_after_injected_infrastructure_failure(manifest):
    evidence = pilot.dry_run(manifest, fail_at=3)
    assert evidence["stop"]["reason"] == "INJECTED_INFRASTRUCTURE_FAILURE"
    assert len(evidence["records_before_stop"]) == 2


def test_model_result_failures_do_not_replace_or_rerun_cases(manifest):
    planned = [case["case_id"] for case in manifest["case_order"]]
    evidence = pilot.dry_run(manifest, fail_at=3)
    observed = [record["case_id"] for record in evidence["records_before_stop"]]
    assert observed == planned[:2]
    assert manifest["selection"]["post_freeze_substitution"] is False


def test_evidence_records_bind_to_one_manifest_and_candidate_hash(manifest):
    result = pilot.public_case_record(manifest, manifest["case_order"][0])
    pilot.validate_case_result(result, manifest)
    changed = copy.deepcopy(result)
    changed["campaign_manifest_hash"] = "0" * 64
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(changed, manifest)


def test_default_mode_is_validation_only(manifest, monkeypatch):
    called = []
    monkeypatch.setattr(pilot, "run_qualification", lambda value: called.append(value))
    assert pilot.main(["--manifest", str(REPO_ROOT / "research" / "quixbugs" / "PAIRED_PILOT_V1.json")]) == 0
    assert called == []


def test_public_record_contains_required_pdb_fields(manifest):
    result = pilot.public_case_record(manifest, manifest["case_order"][0])
    for key in ("pdb_gate_decisions", "pdb_sessions_started", "successful_pdb_observations", "failed_pdb_observations"):
        assert key in result


def test_case_order_hash_uses_task_and_policy(manifest):
    for case in manifest["case_order"]:
        assert case["case_hash"] == pilot.sha256_text(f"{pilot.CAMPAIGN_ID}:{case['task_id']}:{case['policy']}")


def test_frozen_route_refuses_substitution(manifest):
    observation = {**manifest["route"], "input_price": 0, "output_price": 0, "paid_fallback_used": False, "alternate_provider_used": False, "ollama_used": False}
    changed = dict(observation, variant="default")
    with pytest.raises(pilot.PilotError):
        pilot.validate_route_observation(changed, manifest["route"])


def _fake_pdb_result(**changes):
    values = {
        "task_id": "fake-task", "verdict": "REACHABILITY_CASE_FAILED",
        "quixbugs_preflight": SimpleNamespace(authorized=True),
        "contained_preflight": SimpleNamespace(authorized=True),
        "gate_decisions": [], "pdb_observations": {"successful_pdb_observation_count": 0, "failed_pdb_observation_count": 0},
        "events_valid": False, "events_jsonl": "", "sequence_evidence": None,
        "launch_plan": None, "pdb_runtime_bundle_hashes": None, "cleanup_succeeded": True,
        "canonical_source_unchanged": True, "diagnostics": (),
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_zero_count_pdb_mapping_does_not_pass():
    accounting = pilot._pdb_accounting(_fake_pdb_result())
    assert accounting["successful_observations"] == 0
    assert accounting["failed_observations"] == 0
    assert accounting["sessions_started"] == 0
    assert accounting["qualification_passes"] is False


def test_reachability_failed_verdict_does_not_pass():
    result = _fake_pdb_result(pdb_observations={"successful_pdb_observation_count": 1, "failed_pdb_observation_count": 0}, gate_decisions=[{"allowed": True}], verdict="REACHABILITY_CASE_FAILED")
    accounting = pilot._pdb_accounting(result)
    assert accounting["successful_observations"] == 1
    assert accounting["qualification_passes"] is False


def test_no_allowed_gate_decision_does_not_pass():
    result = _fake_pdb_result(pdb_observations={"successful_pdb_observation_count": 1, "failed_pdb_observation_count": 0}, gate_decisions=[{"allowed": False}], verdict="REACHABILITY_CASE_PASSED")
    assert pilot._pdb_accounting(result)["qualification_passes"] is False


def test_invalid_event_or_sequence_evidence_does_not_pass():
    result = _fake_pdb_result(pdb_observations={"successful_pdb_observation_count": 1, "failed_pdb_observation_count": 0}, gate_decisions=[{"allowed": True}], verdict="REACHABILITY_CASE_PASSED", events_valid=False, sequence_evidence={"ok": True})
    assert pilot._pdb_accounting(result)["qualification_passes"] is False


def test_diagnostics_or_cleanup_failure_does_not_pass():
    result = _fake_pdb_result(pdb_observations={"successful_pdb_observation_count": 1, "failed_pdb_observation_count": 0}, gate_decisions=[{"allowed": True}], verdict="REACHABILITY_CASE_PASSED", diagnostics=("failure",), cleanup_succeeded=False)
    assert pilot._pdb_accounting(result)["qualification_passes"] is False


def test_wrong_task_runtime_probe_is_rejected(manifest):
    task = next(item for item in manifest["inventory"] if item["task_id"] == "quixbugs-bucketsort-smoke-v1")
    wrong = SimpleNamespace(module_path="python_programs/flatten.py", focus_function="flatten")
    with pytest.raises(pilot.PilotError):
        pilot.validate_runtime_probe(wrong, pilot.load_manifest(Path(task["manifest_path"])), Path("C:/does-not-matter"))


def test_all_seven_non_gcd_tasks_receive_real_screening(manifest):
    evidence = json.loads((REPO_ROOT / manifest["qualification_evidence_path"]).read_text(encoding="utf-8"))
    assert len(evidence["screening"]) == 7
    assert all(item["screening_evidence"]["screening_passed"] for item in evidence["screening"])


def test_unsupported_pass_status_cannot_enter_ranking(manifest):
    inventory = copy.deepcopy(manifest["inventory"])
    item = next(item for item in inventory if item["task_id"] == "quixbugs-bucketsort-smoke-v1")
    item.pop("qualification_evidence_ref", None)
    assert "quixbugs-bucketsort-smoke-v1" not in {entry["task_id"] for entry in pilot.selection_ranking(inventory)}


def test_inventory_hash_mismatch_is_rejected(manifest):
    changed = copy.deepcopy(manifest)
    changed["inventory"][0]["source_sha256"] = "0" * 64
    with pytest.raises(pilot.PilotError):
        pilot.validate_manifest(changed)


def test_evaluator_infrastructure_error_is_not_buggy_candidate_rejection():
    from agentic_debugger.evaluation.runner import EvaluationStatus
    result = SimpleNamespace(status=EvaluationStatus.INTERNAL_ERROR, outcome=None, workspace=SimpleNamespace(lifecycle=None, prepared=False, cleanup_attempted=False, cleaned=False, canonical_fixture_unchanged=False, error="x"), baseline=SimpleNamespace(valid=False), post_patch_f2p=(), post_patch_p2p=(), timeout=True, diagnostic="error")
    ok, _ = pilot._evaluator_lifecycle(result, "NO_OP")
    assert ok is False


def test_all_result_required_fields_are_enforced(manifest):
    result = pilot.public_case_record(manifest, manifest["case_order"][0])
    del result["transport_evidence"]
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest)


@pytest.mark.parametrize("field", ["task_id", "policy", "order_index"])
def test_wrong_case_identity_is_rejected(manifest, field):
    result = pilot.public_case_record(manifest, manifest["case_order"][0])
    result[field] = "wrong" if field != "order_index" else 99
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest)


def test_wrong_top_level_route_identity_is_rejected(manifest):
    result = pilot.public_case_record(manifest, manifest["case_order"][0])
    result["provider"] = "other"
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest)


def test_every_budget_excess_is_rejected(manifest):
    fields = {
        "logical_model_calls": "max_logical_model_calls", "provider_process_attempts": "max_total_provider_process_attempts",
        "retries": "max_total_transport_retries", "valid_directives": "max_accepted_directives",
        "bounded_directive_feedback_events": "max_malformed_directive_feedback_cycles", "hypotheses_created": "max_hypotheses",
        "patch_submissions": "max_patch_submissions", "verifier_runs": "max_verifier_runs", "public_evidence_bytes": "max_public_evidence_bytes",
    }
    for actual, limit in fields.items():
        result = pilot.public_case_record(manifest, manifest["case_order"][0])
        result[actual] = manifest["budgets"][limit] + 1
        with pytest.raises(pilot.PilotError):
            pilot.validate_case_result(result, manifest)


def test_invalid_hashes_and_commits_are_rejected(manifest):
    result = pilot.public_case_record(manifest, manifest["case_order"][0])
    result["source_hash"] = "bad"
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest)
    result = pilot.public_case_record(manifest, manifest["case_order"][0])
    result["campaign_commit"] = manifest["planning_baseline_commit"]
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest)


def test_terminal_status_contradiction_is_rejected(manifest):
    result = pilot.public_case_record(manifest, manifest["case_order"][0], terminal_status="RESOLVED")
    result["patch_submissions"] = 1
    result["candidate_hash"] = "a" * 64
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest)


def test_static_policy_pdb_activity_is_rejected(manifest):
    result = pilot.public_case_record(manifest, next(case for case in manifest["case_order"] if case["policy"] == "static-baseline"))
    result["pdb_counts"]["allowed_gate_openings"] = 1
    result["pdb_counts"]["total_gate_decisions"] = 1
    result["pdb_gate_decisions"] = [{"allowed": True}]
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest)


def test_route_catalog_version_and_pricing_mismatch_are_rejected(manifest):
    for field, value in (("opencode_version", 7), ("catalog_fingerprint", "bad"), ("input_price", 1)):
        observation = dict(pilot.public_case_record(manifest, manifest["case_order"][0])["route_observation"])
        observation[field] = value
        with pytest.raises(pilot.PilotError):
            pilot.validate_route_observation(observation, manifest["route"])


def test_planning_baseline_is_not_campaign_execution_commit(manifest):
    result = pilot.public_case_record(manifest, manifest["case_order"][0])
    result["campaign_commit"] = pilot.PLANNING_BASELINE_COMMIT
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest)


def test_no_candidate_case_uses_null_candidate_hash(manifest):
    result = pilot.public_case_record(manifest, manifest["case_order"][0])
    assert result["candidate_hash"] is None
    pilot.validate_case_result(result, manifest)


def test_fresh_fake_case_resources_are_issued_per_case(manifest):
    evidence = pilot.dry_run(manifest, fail_at=None)
    ids = evidence["unique_resource_ids"]
    assert len(ids) == 6
    assert len({item["case_execution_context"] for item in ids}) == 6
    assert len({item["owned_workspace"] for item in ids}) == 6


def test_model_failure_advances_once_without_rerun(manifest):
    evidence = pilot.dry_run(manifest, fail_at=None, model_failure_at=2)
    assert evidence["ordinary_model_failure_advanced_once"] is True
    assert evidence["no_task_replacement_or_rerun"] is True
    assert evidence["records_before_stop"][1]["terminal_status"] == "INVALID_MODEL_RESPONSE"


def test_no_provider_or_network_path_is_reached(manifest):
    evidence = pilot.dry_run(manifest, fail_at=None)
    assert evidence["provider_processes_started"] == 0
    assert evidence["network_activity"] is False


def test_stored_zero_successful_observations_are_rejected(manifest):
    record = copy.deepcopy(json.loads((REPO_ROOT / manifest["qualification_evidence_path"]).read_text())["screening"][0])
    record["screening_evidence"]["pdb"]["successful_observations"] = 0
    contract_task = next(x for x in manifest["qualification_contract"]["tasks"] if x["task_id"] == record["task_id"])
    with pytest.raises(pilot.PilotError):
        pilot._validate_deep_screening_evidence(manifest, record, contract_task)


def test_stored_failed_reachability_verdict_is_rejected(manifest):
    record = copy.deepcopy(json.loads((REPO_ROOT / manifest["qualification_evidence_path"]).read_text())["screening"][0])
    record["screening_evidence"]["pdb"]["verdict"] = "REACHABILITY_CASE_FAILED"
    contract_task = next(x for x in manifest["qualification_contract"]["tasks"] if x["task_id"] == record["task_id"])
    with pytest.raises(pilot.PilotError):
        pilot._validate_deep_screening_evidence(manifest, record, contract_task)


def test_stored_false_qualification_pass_is_rejected(manifest):
    record = copy.deepcopy(json.loads((REPO_ROOT / manifest["qualification_evidence_path"]).read_text())["screening"][0])
    record["screening_evidence"]["pdb"]["qualification_passes"] = False
    contract_task = next(x for x in manifest["qualification_contract"]["tasks"] if x["task_id"] == record["task_id"])
    with pytest.raises(pilot.PilotError):
        pilot._validate_deep_screening_evidence(manifest, record, contract_task)


def test_contract_hash_and_evidence_digest_bind_qualification(manifest):
    changed = copy.deepcopy(manifest)
    changed["qualification_contract"]["tasks"][0]["source_sha256"] = "0" * 64
    with pytest.raises(pilot.PilotError):
        pilot.validate_manifest(changed)
    changed = copy.deepcopy(manifest)
    changed["qualification_evidence_sha256"] = "0" * 64
    with pytest.raises(pilot.PilotError):
        pilot.validate_manifest(changed)


def test_wrong_stored_runtime_probe_and_verifier_lifecycle_are_rejected(manifest):
    evidence = json.loads((REPO_ROOT / manifest["qualification_evidence_path"]).read_text())
    record = copy.deepcopy(evidence["screening"][0])
    record["screening_evidence"]["runtime_probe"]["module_path"] = "python_programs/flatten.py"
    contract_task = next(x for x in manifest["qualification_contract"]["tasks"] if x["task_id"] == record["task_id"])
    with pytest.raises(pilot.PilotError):
        pilot._validate_deep_screening_evidence(manifest, record, contract_task)
    record = copy.deepcopy(evidence["screening"][0])
    record["screening_evidence"]["independent_verifier"]["status"] = "INTERNAL_ERROR"
    with pytest.raises(pilot.PilotError):
        pilot._validate_deep_screening_evidence(manifest, record, contract_task)


def _live_authorization(manifest):
    return {
        "authorize_live": True,
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "qualification_contract_hash": manifest["qualification_contract_hash"],
        "accepted_campaign_commit": "a" * 40,
        "permitted_case_ids": [case["case_id"] for case in manifest["case_order"]],
        "expected_opencode_version": "1.0.0",
        "expected_catalog_fingerprint": "c" * 64,
        "zero_price_required": True,
        "no_fallback_required": True,
        **{key: manifest["route"][key] for key in ("provider", "model", "variant", "protocol")},
    }


def _live_result(manifest):
    result = pilot.public_case_record(manifest, manifest["case_order"][0])
    result.update({
        "execution_kind": "LIVE_CASE",
        "campaign_commit": "a" * 40,
        "accepted_code_commit": "a" * 40,
        "public_request_hash": "b" * 64,
        "source_hash": next(item["source_sha256"] for item in manifest["inventory"] if item["task_id"] == result["task_id"]),
        "terminal_status": "UNRESOLVED", "baseline_reproduction": True, "logical_model_calls": 1, "provider_process_attempts": 1, "valid_directives": 1,
        "independent_verifier_result": {"status": "COMPLETED", "outcome": "NO_OP", "lifecycle_succeeded": True},
        "transport_evidence": {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False},
        "terminal_reason_code": "UNRESOLVED_COMPLETED",
        "terminal_transport_evidence": {"final_attempt_classification": "COMPLETED_RESPONSE", "process_exit_code": 0, "timed_out": False, "provider_error_category": None, "provider_completed_response": True, "evidence_reference": "test"},
    })
    result["route_observation"].update({"opencode_version": "1.0.0", "active_model_status": "ACTIVE", "variant_available": True, "catalog_fingerprint": "c" * 64, "preflight_success": True})
    return result


def test_live_case_requires_authorized_real_identity(manifest):
    result = _live_result(manifest)
    auth = _live_authorization(manifest)
    pilot.validate_case_result(result, manifest, auth)
    for key, value in (("campaign_commit", None), ("public_request_hash", None), ("source_hash", None)):
        changed = copy.deepcopy(result)
        changed[key] = value
        with pytest.raises(pilot.PilotError):
            pilot.validate_case_result(changed, manifest, auth)
    changed = copy.deepcopy(result)
    changed["transport_evidence"]["synthetic"] = True
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(changed, manifest, auth)


def test_live_case_source_route_and_cost_are_exact(manifest):
    auth = _live_authorization(manifest)
    for field, value in (("source_revision", "0" * 40), ("source_hash", "d" * 64), ("provider_reported_cost", 0.01)):
        changed = _live_result(manifest)
        changed[field] = value
        with pytest.raises(pilot.PilotError):
            pilot.validate_case_result(changed, manifest, auth)


def test_actual_gate_decisions_and_lifecycle_counters_are_consistent(manifest):
    result = pilot.public_case_record(manifest, next(case for case in manifest["case_order"] if case["policy"] == "pdb-on-uncertainty"))
    result["pdb_gate_decisions"] = [{"allowed": True, "reason": "allowed"}]
    result["pdb_counts"].update({"total_gate_decisions": 1, "allowed_gate_openings": 1, "sessions_started": 0, "successful_observations": 1})
    result["successful_pdb_observations"] = 1
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest)
    result = pilot.public_case_record(manifest, manifest["case_order"][0])
    result["pdb_gate_decisions"] = [{"allowed": False, "reason": "rejected"}]
    result["pdb_counts"].update({"total_gate_decisions": 1, "rejected_gate_decisions": 1})
    result["pdb_counts"]["sessions_started"] = 1
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest)


def test_dynamic_transport_and_wall_clock_budgets_are_enforced(manifest):
    result = pilot.public_case_record(manifest, manifest["case_order"][0])
    result.update({"logical_model_calls": 1, "provider_process_attempts": 4})
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest)
    result = pilot.public_case_record(manifest, manifest["case_order"][0])
    result["wall_clock_duration_seconds"] = manifest["budgets"]["total_case_timeout_seconds"] + 1
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest)


def test_execution_kind_cannot_be_forged_as_live_or_dry(manifest):
    result = pilot.public_case_record(manifest, manifest["case_order"][0])
    result["execution_kind"] = "LIVE_CASE"
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest)


def test_direct_generic_pdb_boundary_rejects_other_task_probe(manifest, tmp_path):
    from agentic_debugger.demo.catalog import RuntimeProbe
    from agentic_debugger.quixbugs.contained_pdb import ContainedPdbError, run_quixbugs_pdb_reachability_case
    task = next(item for item in manifest["inventory"] if item["task_id"] == "quixbugs-bucketsort-smoke-v1")
    probe = RuntimeProbe("python_programs/flatten.py", "flatten", "list(flatten([[1], 2]))", "yield flatten(x)", ("arr", "x"))
    with pytest.raises(ContainedPdbError):
        run_quixbugs_pdb_reachability_case(repository_root=str(REPO_ROOT), manifest_path=str(REPO_ROOT / task["manifest_path"]), sources_parent=str(tmp_path), facts=None, resource_limits=None, runtime_probe=probe, hypothesis_id="test", hypothesis_statement="test")


def test_coordinated_fake_source_and_test_hashes_fail_independent_authority(manifest):
    changed = copy.deepcopy(manifest)
    fake = "f" * 64
    for item in changed["inventory"]:
        item["source_sha256"] = fake
        item["test_sha256"] = fake
    for item in changed["qualification_contract"]["tasks"]:
        item["source_sha256"] = fake
        item["test_sha256"] = fake
    changed["qualification_contract_hash"] = pilot.sha256_text(pilot.canonical_json(changed["qualification_contract"]))
    with pytest.raises(pilot.PilotError):
        pilot.validate_manifest(changed)


def test_source_integrity_authority_digest_path_and_task_set_are_fail_closed(manifest, monkeypatch, tmp_path):
    changed = copy.deepcopy(manifest)
    changed["source_integrity_authority"]["sha256"] = "0" * 64
    with pytest.raises(pilot.PilotError):
        pilot.validate_manifest(changed)
    authority = json.loads((REPO_ROOT / "research/quixbugs/QUIXBUGS_SOURCE_INTEGRITY_V1.json").read_text())
    authority["tasks"] = authority["tasks"][:-1]
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(authority), encoding="utf-8")
    monkeypatch.setattr(pilot, "SOURCE_INTEGRITY_PATH", path)
    monkeypatch.setattr(pilot, "SOURCE_INTEGRITY_SHA256", pilot.file_hash(path))
    with pytest.raises(pilot.PilotError):
        pilot._load_source_integrity_authority()
    authority = json.loads((REPO_ROOT / "research/quixbugs/QUIXBUGS_SOURCE_INTEGRITY_V1.json").read_text())
    authority["tasks"].append(copy.deepcopy(authority["tasks"][0]))
    path.write_text(json.dumps(authority), encoding="utf-8")
    monkeypatch.setattr(pilot, "SOURCE_INTEGRITY_SHA256", pilot.file_hash(path))
    with pytest.raises(pilot.PilotError):
        pilot._load_source_integrity_authority()


def test_source_integrity_repository_and_revision_are_frozen(manifest):
    for field, value in (("repository", "https://example.invalid/QuixBugs"), ("revision", "0" * 40)):
        changed = copy.deepcopy(manifest)
        changed["authority"][field] = value
        with pytest.raises(pilot.PilotError):
            pilot.validate_manifest(changed)


@pytest.mark.parametrize("field", ["test_sha256", "implementation_path", "test_path"])
def test_source_integrity_field_mutation_is_rejected(manifest, field):
    changed = copy.deepcopy(manifest)
    changed["inventory"][0][field] = "0" * 64 if field == "test_sha256" else "python_programs/other.py"
    with pytest.raises(pilot.PilotError):
        pilot.validate_manifest(changed)


def _resolved_live_result(manifest):
    result = _live_result(manifest)
    result.update({
        "terminal_status": "RESOLVED", "baseline_reproduction": True,
        "logical_model_calls": 1, "valid_directives": 1, "patch_submissions": 1,
        "candidate_hash": "d" * 64, "verifier_runs": 1,
        "independent_verifier_result": {"status": "COMPLETED", "outcome": "RESOLVED", "lifecycle_succeeded": True},
        "transport_evidence": {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False},
    })
    result["terminal_transport_evidence"] = {"final_attempt_classification": "COMPLETED_RESPONSE", "process_exit_code": 0, "timed_out": False, "provider_error_category": None, "provider_completed_response": True, "evidence_reference": "test"}
    return result


def test_terminal_matrix_rejects_resolved_missing_baseline_call_directive_or_clean_transport(manifest):
    for mutation in (
        {"baseline_reproduction": False},
        {"logical_model_calls": 0},
        {"valid_directives": 0},
        {"transport_evidence": {"completed_response": True, "malformed_response": True, "provider_error": False, "synthetic": False}},
    ):
        result = _resolved_live_result(manifest)
        result.update(mutation)
        with pytest.raises(pilot.PilotError):
            pilot.validate_case_result(result, manifest, _live_authorization(manifest))


def _invalid_live_result(manifest):
    result = _live_result(manifest)
    result.update({"terminal_status": "INVALID_MODEL_RESPONSE", "logical_model_calls": 1, "provider_process_attempts": 1, "valid_directives": 1, "malformed_directive_rejections": 1, "bounded_directive_feedback_events": 1, "transport_evidence": {"completed_response": True, "malformed_response": True, "provider_error": False, "synthetic": False}})
    result["terminal_transport_evidence"] = {"final_attempt_classification": "MALFORMED_RESPONSE", "process_exit_code": 0, "timed_out": False, "provider_error_category": None, "provider_completed_response": True, "evidence_reference": "test"}
    return result


def test_invalid_model_response_requires_malformed_evidence_and_not_resolved(manifest):
    result = _invalid_live_result(manifest)
    pilot.validate_case_result(result, manifest, _live_authorization(manifest))
    for mutation in ({"malformed_directive_rejections": 0}, {"independent_verifier_result": {"status": "COMPLETED", "outcome": "RESOLVED", "lifecycle_succeeded": True}}):
        changed = copy.deepcopy(result)
        changed.update(mutation)
        with pytest.raises(pilot.PilotError):
            pilot.validate_case_result(changed, manifest, _live_authorization(manifest))


def test_unresolved_requires_baseline_and_nonresolved_completed_verifier(manifest):
    result = _live_result(manifest)
    result.update({"terminal_status": "UNRESOLVED", "baseline_reproduction": True, "logical_model_calls": 1, "transport_evidence": {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False}, "independent_verifier_result": {"status": "COMPLETED", "outcome": "NO_OP", "lifecycle_succeeded": True}})
    pilot.validate_case_result(result, manifest, _live_authorization(manifest))
    for mutation in ({"baseline_reproduction": False}, {"independent_verifier_result": {"status": "COMPLETED", "outcome": "RESOLVED", "lifecycle_succeeded": True}}):
        changed = copy.deepcopy(result)
        changed.update(mutation)
        with pytest.raises(pilot.PilotError):
            pilot.validate_case_result(changed, manifest, _live_authorization(manifest))


def test_pdb_activity_requires_pdb_policy_baseline_and_hypothesis(manifest):
    case = next(case for case in manifest["case_order"] if case["policy"] == "pdb-on-uncertainty")
    for mutation in ({"hypotheses_created": 0}, {"baseline_reproduction": False}):
        result = pilot.public_case_record(manifest, case)
        result["pdb_gate_decisions"] = [{"allowed": True, "reason": "allowed"}]
        result["pdb_counts"].update({"total_gate_decisions": 1, "allowed_gate_openings": 1, "sessions_started": 1, "successful_observations": 1})
        result["pdb_sessions_started"] = 1
        result["successful_pdb_observations"] = 1
        result["baseline_reproduction"] = True
        result["hypotheses_created"] = 1
        result.update(mutation)
        with pytest.raises(pilot.PilotError):
            pilot.validate_case_result(result, manifest)


def test_structured_infrastructure_and_provider_evidence_are_required(manifest):
    result = _live_result(manifest)
    result["terminal_status"] = "INFRASTRUCTURE_ERROR"
    result["infrastructure_evidence"] = {"stage": "containment", "reason_code": "CONTAINMENT_FAILURE", "confirmed_failure": False, "classification": "containment", "evidence_reference": "test"}
    result["termination_reason"] = "INFRA failure text only"
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest))
    result = _live_result(manifest)
    result["terminal_status"] = "PROVIDER_ERROR"
    result["transport_evidence"] = {"completed_response": False, "malformed_response": False, "provider_error": True, "synthetic": False}
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest))


def test_malformed_feedback_and_rejection_counts_must_agree(manifest):
    result = _invalid_live_result(manifest)
    result["bounded_directive_feedback_events"] = 0
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest))


def _preprovider_result(manifest, reason, *, provider="OpenCode Zen", model="opencode/deepseek-v4-flash-free", variant="max", active="INACTIVE", available=True, input_price=0, output_price=0):
    result = pilot.public_case_record(manifest, manifest["case_order"][0])
    result.update({
        "execution_kind": "LIVE_CASE", "campaign_commit": "a" * 40, "accepted_code_commit": "a" * 40,
        "provider": provider, "model": model, "variant": variant, "public_request_hash": None,
        "source_hash": None, "terminal_status": "BLOCKED", "terminal_reason_code": reason,
        "terminal_transport_evidence": {"final_attempt_classification": "PRE_PROVIDER_BLOCK", "process_exit_code": None, "timed_out": False, "provider_error_category": None, "provider_completed_response": False, "evidence_reference": "test-preflight"},
        "blocked_evidence": {"block_kind": "live-pre-provider", "reason_code": reason, "confirmed": True, "evidence_reference": "test-preflight"},
        "transport_evidence": {"completed_response": False, "malformed_response": False, "provider_error": False, "synthetic": False},
    })
    result["route_observation"].update({"provider": provider, "model": model, "variant": variant, "active_model_status": active, "variant_available": available, "input_price": input_price, "output_price": output_price, "preflight_success": False})
    evidence = {field: None for field in pilot.PREFLIGHT_FAILURE_FIELDS}
    evidence.update({"failure_category": reason, "evidence_reference": "test-preflight"})
    expected = manifest["route"]
    if reason == "PROVIDER_MISMATCH":
        evidence.update({"expected_provider": expected["provider"], "observed_provider": provider})
    elif reason == "MODEL_MISMATCH":
        evidence.update({"expected_provider": expected["provider"], "observed_provider": provider, "expected_model": expected["model"], "observed_model": model})
    elif reason == "VARIANT_MISMATCH":
        evidence.update({"expected_provider": expected["provider"], "observed_provider": provider, "expected_model": expected["model"], "observed_model": model, "expected_variant": expected["variant"], "observed_variant": variant})
    elif reason == "MODEL_INACTIVE":
        evidence.update({"expected_provider": expected["provider"], "observed_provider": provider, "expected_model": expected["model"], "observed_model": model, "expected_variant": expected["variant"], "observed_variant": variant, "expected_protocol": expected["protocol"], "observed_protocol": result["route_observation"]["protocol"], "observed_active_model_status": active})
    elif reason == "VARIANT_UNAVAILABLE":
        evidence.update({"expected_provider": expected["provider"], "observed_provider": provider, "expected_model": expected["model"], "observed_model": model, "expected_variant": expected["variant"], "observed_variant": variant, "expected_protocol": expected["protocol"], "observed_protocol": result["route_observation"]["protocol"], "observed_active_model_status": active, "observed_variant_available": available})
    elif reason == "NONZERO_PRICING":
        evidence.update({"expected_provider": expected["provider"], "observed_provider": provider, "expected_model": expected["model"], "observed_model": model, "expected_variant": expected["variant"], "observed_variant": variant, "expected_protocol": expected["protocol"], "observed_protocol": result["route_observation"]["protocol"], "observed_active_model_status": active, "observed_variant_available": available, "observed_input_price": input_price, "observed_output_price": output_price})
    result["preflight_failure_evidence"] = evidence
    return result


def test_truthful_live_preprovider_route_failures_validate(manifest):
    cases = [
        _preprovider_result(manifest, "MODEL_INACTIVE"),
        _preprovider_result(manifest, "VARIANT_UNAVAILABLE", active="ACTIVE", available=False),
        _preprovider_result(manifest, "NONZERO_PRICING", active="ACTIVE", input_price=0.01),
        _preprovider_result(manifest, "PROVIDER_MISMATCH", provider="Other Provider", active="ACTIVE"),
        _preprovider_result(manifest, "MODEL_MISMATCH", model="other/model", active="ACTIVE"),
    ]
    auth = _live_authorization(manifest)
    for result in cases:
        pilot.validate_case_result(result, manifest, auth)


def test_preprovider_route_mismatch_reason_and_block_kind_are_exact(manifest):
    result = _preprovider_result(manifest, "MODEL_INACTIVE")
    result["terminal_reason_code"] = "VARIANT_UNAVAILABLE"
    result["blocked_evidence"]["reason_code"] = "VARIANT_UNAVAILABLE"
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest))
    result = _preprovider_result(manifest, "MODEL_INACTIVE")
    result["blocked_evidence"]["block_kind"] = "dry-run"
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest))
    result = pilot.public_case_record(manifest, manifest["case_order"][0])
    result["blocked_evidence"]["block_kind"] = "live-pre-provider"
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest)


def test_preprovider_block_has_no_provider_or_case_activity(manifest):
    result = _preprovider_result(manifest, "MODEL_INACTIVE")
    result["logical_model_calls"] = 1
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest))


def _pdb_not_reached_result(manifest, *, rejected=False):
    case = next(case for case in manifest["case_order"] if case["policy"] == "pdb-on-uncertainty")
    result = pilot.public_case_record(manifest, case)
    entry = next(item for item in manifest["inventory"] if item["task_id"] == case["task_id"])
    result.update({"execution_kind": "LIVE_CASE", "campaign_commit": "a" * 40, "accepted_code_commit": "a" * 40, "public_request_hash": "b" * 64, "source_hash": entry["source_sha256"], "terminal_status": "PDB_NOT_REACHED", "terminal_reason_code": "PDB_NOT_REACHED_GATE_REJECTED" if rejected else "PDB_NOT_REACHED_NO_GATE", "baseline_reproduction": True, "logical_model_calls": 1, "provider_process_attempts": 1, "valid_directives": 1, "independent_verifier_result": {"status": "COMPLETED", "outcome": "NO_OP", "lifecycle_succeeded": True}, "transport_evidence": {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False}, "terminal_transport_evidence": {"final_attempt_classification": "COMPLETED_RESPONSE", "process_exit_code": 0, "timed_out": False, "provider_error_category": None, "provider_completed_response": True, "evidence_reference": "test-pdb"}})
    result["route_observation"].update({"opencode_version": "1.0.0", "active_model_status": "ACTIVE", "variant_available": True, "catalog_fingerprint": "c" * 64, "preflight_success": True})
    if rejected:
        result["pdb_gate_decisions"] = [{"allowed": False, "reason": "rejected"}]
        result["pdb_counts"]["total_gate_decisions"] = 1
        result["pdb_counts"]["rejected_gate_decisions"] = 1
    return result


def test_pdb_not_reached_no_gate_and_rejected_gate_contracts(manifest):
    auth = _live_authorization(manifest)
    pilot.validate_case_result(_pdb_not_reached_result(manifest), manifest, auth)
    pilot.validate_case_result(_pdb_not_reached_result(manifest, rejected=True), manifest, auth)
    for mutation in ({"baseline_reproduction": False}, {"transport_evidence": {"completed_response": True, "malformed_response": True, "provider_error": False, "synthetic": False}}, {"transport_evidence": {"completed_response": False, "malformed_response": False, "provider_error": True, "synthetic": False}}):
        result = _pdb_not_reached_result(manifest)
        result.update(mutation)
        with pytest.raises(pilot.PilotError):
            pilot.validate_case_result(result, manifest, auth)


def test_terminal_attempt_contradictions_are_rejected(manifest):
    auth = _live_authorization(manifest)
    for classification, timed_out, exit_code in (("TIMEOUT", True, None), ("MALFORMED_RESPONSE", False, 0), ("COMPLETED_RESPONSE", False, 1)):
        result = _resolved_live_result(manifest)
        result["terminal_transport_evidence"].update({"final_attempt_classification": classification, "timed_out": timed_out, "process_exit_code": exit_code})
        with pytest.raises(pilot.PilotError):
            pilot.validate_case_result(result, manifest, auth)


def _all_structured_preprovider_result(manifest, reason):
    kwargs = {"active": "ACTIVE", "available": True}
    if reason == "PROVIDER_MISMATCH": kwargs["provider"] = "Other Provider"
    if reason == "MODEL_MISMATCH": kwargs["model"] = "other/model"
    if reason == "VARIANT_MISMATCH": kwargs["variant"] = "other"
    if reason == "PROTOCOL_MISMATCH": kwargs["protocol"] = "2.0"
    if reason == "MODEL_INACTIVE": kwargs["active"] = "INACTIVE"
    if reason == "VARIANT_UNAVAILABLE": kwargs["available"] = False
    if reason == "NONZERO_PRICING": kwargs["input_price"] = 0.01
    protocol = kwargs.pop("protocol", "1.3")
    result = _preprovider_result(manifest, "MODEL_INACTIVE", **kwargs)
    result["route_observation"]["protocol"] = protocol
    result["terminal_reason_code"] = reason
    result["blocked_evidence"]["reason_code"] = reason
    if reason == "PAID_FALLBACK_REQUIRED": result["route_observation"]["paid_fallback_used"] = True
    if reason == "ALTERNATE_PROVIDER_REQUIRED": result["route_observation"]["alternate_provider_used"] = True
    if reason == "CATALOG_PREFLIGHT_FAILED":
        result["route_observation"].update({"provider": manifest["route"]["provider"], "model": manifest["route"]["model"], "variant": manifest["route"]["variant"], "active_model_status": "NOT_RUN", "variant_available": False, "input_price": 0, "output_price": 0, "paid_fallback_used": False, "alternate_provider_used": False, "preflight_success": False})
    if reason == "OPENCODE_VERSION_MISMATCH": result["route_observation"]["opencode_version"] = "2.0.0"
    evidence = {field: None for field in pilot.PREFLIGHT_FAILURE_FIELDS}
    evidence.update({"failure_category": reason, "evidence_reference": "test-structured-preflight"})
    route = result["route_observation"]
    expected = manifest["route"]
    for key in ("provider", "model", "variant", "protocol"):
        if key in {"provider", "model", "variant", "protocol"}:
            evidence[f"expected_{key}"] = expected[key]
            evidence[f"observed_{key}"] = route[key]
    if reason == "PROTOCOL_MISMATCH": pass
    elif reason == "MODEL_INACTIVE": evidence["observed_active_model_status"] = route["active_model_status"]
    elif reason == "VARIANT_UNAVAILABLE": evidence.update({"observed_active_model_status": route["active_model_status"], "observed_variant_available": route["variant_available"]})
    elif reason == "NONZERO_PRICING": evidence.update({"observed_active_model_status": route["active_model_status"], "observed_variant_available": route["variant_available"], "observed_input_price": route["input_price"], "observed_output_price": route["output_price"]})
    elif reason == "PAID_FALLBACK_REQUIRED": evidence["paid_fallback_required"] = True
    elif reason == "ALTERNATE_PROVIDER_REQUIRED": evidence["alternate_provider_required"] = True
    elif reason == "CATALOG_PREFLIGHT_FAILED": evidence.update({"catalog_failure_category": "CATALOG_UNAVAILABLE", "catalog_failure_error": "catalog lookup failed"})
    elif reason == "OPENCODE_VERSION_MISMATCH": evidence = {"failure_category": reason, "expected_opencode_version": "1.0.0", "observed_opencode_version": "2.0.0", "evidence_reference": "test-structured-preflight"}
    elif reason == "MANIFEST_HASH_CHANGED": evidence = {"failure_category": reason, "expected_manifest_hash": pilot.manifest_hash(manifest), "observed_manifest_hash": "0" * 64, "evidence_reference": "test-structured-preflight"}
    elif reason == "QUALIFICATION_CONTRACT_CHANGED": evidence = {"failure_category": reason, "expected_qualification_contract_hash": manifest["qualification_contract_hash"], "observed_qualification_contract_hash": "0" * 64, "evidence_reference": "test-structured-preflight"}
    elif reason == "TRACKED_SOURCE_CHANGED": evidence = {"failure_category": reason, "expected_source_authority_hash": pilot.SOURCE_INTEGRITY_SHA256, "observed_source_authority_hash": "0" * 64, "evidence_reference": "test-structured-preflight"}
    elif reason == "CAMPAIGN_COMMIT_MISMATCH": evidence = {"failure_category": reason, "expected_campaign_commit": "a" * 40, "observed_campaign_commit": "b" * 40, "evidence_reference": "test-structured-preflight"}
    elif reason == "LIVE_AUTHORIZATION_INVALID": evidence = {"failure_category": reason, "authorization_artifact_hash": None, "authorization_validation_error": "MISSING_AUTHORIZATION", "evidence_reference": "test-structured-preflight"}
    evidence = {field: evidence.get(field) if field in pilot.PRE_PROVIDER_REASON_FIELDS[reason] or field == "failure_category" else None for field in pilot.PREFLIGHT_FAILURE_FIELDS}
    result["preflight_failure_evidence"] = evidence
    return result


@pytest.mark.parametrize("reason", sorted(pilot.LIVE_PRE_PROVIDER_REASON_CODES))
def test_every_declared_preprovider_failure_has_structured_evidence(manifest, reason):
    result = _all_structured_preprovider_result(manifest, reason)
    pilot.validate_case_result(result, manifest, None if reason == "LIVE_AUTHORIZATION_INVALID" else _live_authorization(manifest))


def test_preprovider_predicates_do_not_overlap_or_infer_catalog_or_version(manifest):
    result = _all_structured_preprovider_result(manifest, "MODEL_INACTIVE")
    result["terminal_reason_code"] = "CATALOG_PREFLIGHT_FAILED"
    result["blocked_evidence"]["reason_code"] = "CATALOG_PREFLIGHT_FAILED"
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest))
    result = _all_structured_preprovider_result(manifest, "OPENCODE_VERSION_MISMATCH")
    result["preflight_failure_evidence"]["observed_opencode_version"] = "1.0.0"
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest))


def _campaign_stop_result(manifest, reason):
    blocked_case = manifest["case_order"][1]
    trigger_case = manifest["case_order"][0]
    result = pilot.public_case_record(manifest, blocked_case)
    result.update({"execution_kind": "LIVE_CASE", "campaign_commit": "a" * 40, "accepted_code_commit": "a" * 40, "terminal_status": "BLOCKED", "terminal_reason_code": reason, "termination_reason": reason, "blocked_evidence": {"block_kind": "campaign-stop", "reason_code": reason, "confirmed": True, "evidence_reference": "test-campaign-stop"}, "terminal_transport_evidence": {"final_attempt_classification": "CAMPAIGN_STOP", "process_exit_code": None, "timed_out": False, "provider_error_category": None, "provider_completed_response": False, "evidence_reference": "test-campaign-stop"}, "transport_evidence": {"completed_response": False, "malformed_response": False, "provider_error": False, "synthetic": False}})
    evidence = {field: None for field in pilot.CAMPAIGN_STOP_EVIDENCE_FIELDS}
    evidence.update({"reason_code": reason, "evidence_reference": "test-campaign-stop", "confirmed": True})
    prior = {}
    authority = {}
    if reason in {"TRACKED_SOURCE_CHANGED", "MANIFEST_HASH_CHANGED", "QUALIFICATION_CONTRACT_CHANGED"}:
        identity = {"TRACKED_SOURCE_CHANGED": "AUTHORITY_CHECK:TRACKED_SOURCE", "MANIFEST_HASH_CHANGED": "AUTHORITY_CHECK:MANIFEST", "QUALIFICATION_CONTRACT_CHANGED": "AUTHORITY_CHECK:QUALIFICATION_CONTRACT"}[reason]
        if reason == "MANIFEST_HASH_CHANGED":
            record = {"identity": identity, "reason_code": reason, "expected_manifest_hash": pilot.manifest_hash(manifest), "observed_manifest_hash": "0" * 64, "evidence_reference": "test-authority"}
        elif reason == "QUALIFICATION_CONTRACT_CHANGED":
            record = {"identity": identity, "reason_code": reason, "expected_qualification_contract_hash": manifest["qualification_contract_hash"], "observed_qualification_contract_hash": "0" * 64, "evidence_reference": "test-authority"}
        else:
            record = {"identity": identity, "reason_code": reason, "expected_source_authority_hash": pilot.SOURCE_INTEGRITY_SHA256, "observed_source_authority_hash": "0" * 64, "evidence_reference": "test-authority"}
        authority = pilot.validate_authority_checks([record], manifest)
        evidence["pre_case_authority_check_identity"] = identity
        evidence["authority_check_record_sha256"] = authority[identity].sha256
    else:
        trigger = _live_result(manifest)
        trigger["case_id"] = trigger_case["case_id"]
        trigger["task_id"] = trigger_case["task_id"]
        trigger["policy"] = trigger_case["policy"]
        trigger["order_index"] = trigger_case["order_index"]
        trigger.update({"terminal_status": "INFRASTRUCTURE_ERROR", "terminal_reason_code": "INFRASTRUCTURE_FAILURE", "termination_reason": "injected"})
        infra = {"stage": "controller", "reason_code": reason, "confirmed_failure": True, "classification": "CONTROLLER", "terminal_classification": "INFRASTRUCTURE_FAILURE", "provider_attempt_index": None, "prior_lifecycle_completed": True, "source_mutation_observed": False, "expected_source_hash": None, "evidence_reference": "trigger"}
        if reason == "TRANSPORT_EVIDENCE_LOSS":
            infra.update({"stage": "provider_transport", "classification": "PROVIDER_TRANSPORT", "provider_attempt_index": 1})
            trigger["transport_evidence"] = {"completed_response": False, "malformed_response": False, "provider_error": False, "synthetic": False}
            trigger["terminal_transport_evidence"].update({"provider_completed_response": False, "process_exit_code": None})
            trigger["logical_model_calls"] = 1
            trigger["provider_process_attempts"] = 1
            trigger["terminal_transport_evidence"]["final_attempt_classification"] = "INFRASTRUCTURE_FAILURE"
        elif reason == "CLEANUP_FAILURE":
            infra.update({"stage": "cleanup", "classification": "CLEANUP"})
            trigger["transport_evidence"] = {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False}
            trigger["terminal_transport_evidence"].update({"final_attempt_classification": "INFRASTRUCTURE_FAILURE", "provider_completed_response": True, "process_exit_code": 0})
            trigger["logical_model_calls"] = 1
            trigger["provider_process_attempts"] = 1
        elif reason == "CONTAINMENT_UNCERTAINTY":
            infra.update({"stage": "containment_pre_provider", "reason_code": "CONTAINMENT_FAILURE", "classification": "PRE_PROVIDER", "prior_lifecycle_completed": False})
            trigger.update({"logical_model_calls": 0, "provider_process_attempts": 0, "retries": 0, "valid_directives": 0, "malformed_directive_rejections": 0, "bounded_directive_feedback_events": 0, "hypotheses_created": 0, "patch_submissions": 0, "verifier_runs": 0, "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "provider_reported_cost": 0, "public_request_hash": None, "source_hash": None})
            trigger["transport_evidence"] = {"completed_response": False, "malformed_response": False, "provider_error": False, "synthetic": False}
            trigger["route_observation"]["preflight_success"] = False
            trigger["terminal_transport_evidence"].update({"final_attempt_classification": "INFRASTRUCTURE_FAILURE", "provider_completed_response": False, "process_exit_code": None})
        elif reason == "VERIFIER_INTEGRITY_FAILURE":
            infra.update({"stage": "verifier", "classification": "VERIFIER"})
            trigger["transport_evidence"] = {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False}
            trigger["terminal_transport_evidence"].update({"final_attempt_classification": "INFRASTRUCTURE_FAILURE", "provider_completed_response": True, "process_exit_code": 0})
            trigger["logical_model_calls"] = 1
            trigger["provider_process_attempts"] = 1
            trigger["verifier_runs"] = 1
        else:
            trigger["transport_evidence"] = {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False}
            trigger["terminal_transport_evidence"].update({"final_attempt_classification": "INFRASTRUCTURE_FAILURE", "provider_completed_response": True, "process_exit_code": 0})
            trigger["logical_model_calls"] = 1
            trigger["provider_process_attempts"] = 1
        if reason == "SOURCE_MUTATION":
            entry = next(item for item in manifest["inventory"] if item["task_id"] == trigger_case["task_id"])
            trigger["source_hash"] = "0" * 64
            infra.update({"stage": "cleanup", "reason_code": "CLEANUP_FAILURE", "classification": "CLEANUP", "source_mutation_observed": True, "expected_source_hash": entry["source_sha256"]})
        trigger["infrastructure_evidence"] = infra
        evidence["trigger_case_id"] = trigger_case["case_id"]
    if reason == "TRANSPORT_EVIDENCE_LOSS": evidence.update({"expected_evidence_complete": True, "observed_evidence_complete": False})
    elif reason == "CONTAINMENT_UNCERTAINTY": evidence.update({"expected_containment_confirmed": True, "observed_containment_confirmed": False})
    elif reason == "SOURCE_MUTATION":
        entry = next(item for item in manifest["inventory"] if item["task_id"] == trigger_case["task_id"])
        evidence.update({"expected_source_hash": entry["source_sha256"], "observed_source_hash": "0" * 64})
    elif reason == "CLEANUP_FAILURE": evidence.update({"expected_cleanup_succeeded": True, "observed_cleanup_succeeded": False})
    elif reason == "VERIFIER_INTEGRITY_FAILURE": evidence.update({"expected_verifier_integrity": True, "observed_verifier_integrity": False})
    elif reason == "RESULT_SCHEMA_INCONSISTENCY": evidence.update({"schema_error_code": "CASE_ID_MISMATCH"})
    elif reason == "MANIFEST_HASH_CHANGED": evidence.update({"expected_manifest_hash": pilot.manifest_hash(manifest), "observed_manifest_hash": "0" * 64})
    elif reason == "QUALIFICATION_CONTRACT_CHANGED": evidence.update({"expected_qualification_contract_hash": manifest["qualification_contract_hash"], "observed_qualification_contract_hash": "0" * 64})
    elif reason == "TRACKED_SOURCE_CHANGED": evidence.update({"expected_source_authority_hash": pilot.SOURCE_INTEGRITY_SHA256, "observed_source_authority_hash": "0" * 64})
    result["campaign_stop_evidence"] = evidence
    if evidence["trigger_case_id"]:
        prior = pilot.validate_case_results_in_order([trigger], manifest, _live_authorization(manifest))
        evidence["trigger_result_sha256"] = prior[trigger_case["case_id"]].sha256
        result["campaign_stop_evidence"] = evidence
    return result, prior, authority


@pytest.mark.parametrize("reason", sorted(pilot.CAMPAIGN_STOP_REASON_CODES))
def test_every_campaign_stop_reason_requires_structured_evidence(manifest, reason):
    result, prior, authority = _campaign_stop_result(manifest, reason)
    pilot.validate_case_result(result, manifest, _live_authorization(manifest), prior, authority)


def test_campaign_stop_route_and_activity_are_bound(manifest):
    result, prior, authority = _campaign_stop_result(manifest, "CLEANUP_FAILURE")
    result["termination_reason"] = "prose only"
    result["provider"] = "Other Provider"
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest), prior, authority)
    result, prior, authority = _campaign_stop_result(manifest, "CLEANUP_FAILURE")
    result["logical_model_calls"] = 1
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest))


def test_route_identity_and_preprovider_zero_activity_are_exact(manifest):
    result = _live_result(manifest)
    result["route_observation"]["model"] = "other/model"
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest))
    result = _preprovider_result(manifest, "MODEL_INACTIVE")
    result["prompt_tokens"] = 1
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest))
    result = _preprovider_result(manifest, "MODEL_INACTIVE")
    result["provider_reported_cost"] = 0.01
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest))


def test_baseline_reproduction_requires_valid_directive_for_unresolved_and_pdb_not_reached(manifest):
    result = _live_result(manifest)
    result["valid_directives"] = 0
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest))
    result = _pdb_not_reached_result(manifest)
    result["valid_directives"] = 0
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest))


def test_terminal_transport_classification_is_exact(manifest):
    auth = _live_authorization(manifest)
    result = _invalid_live_result(manifest)
    result["terminal_transport_evidence"].update({"timed_out": True, "process_exit_code": None})
    with pytest.raises(pilot.PilotError): pilot.validate_case_result(result, manifest, auth)
    result = _invalid_live_result(manifest)
    result["terminal_transport_evidence"]["process_exit_code"] = 1
    with pytest.raises(pilot.PilotError): pilot.validate_case_result(result, manifest, auth)
    result = _invalid_live_result(manifest)
    result["terminal_transport_evidence"]["provider_error_category"] = "X"
    with pytest.raises(pilot.PilotError): pilot.validate_case_result(result, manifest, auth)
    result = _live_result(manifest)
    result.update({"terminal_status": "PROVIDER_ERROR", "transport_evidence": {"completed_response": False, "malformed_response": False, "provider_error": True, "synthetic": False}, "terminal_transport_evidence": {"final_attempt_classification": "TIMEOUT", "process_exit_code": 0, "timed_out": False, "provider_error_category": None, "provider_completed_response": False, "evidence_reference": "test"}})
    with pytest.raises(pilot.PilotError): pilot.validate_case_result(result, manifest, auth)


def test_preprovider_infrastructure_failure_can_have_zero_calls(manifest):
    result = _live_result(manifest)
    result.update({"terminal_status": "INFRASTRUCTURE_ERROR", "logical_model_calls": 0, "provider_process_attempts": 0, "valid_directives": 0, "public_request_hash": None, "source_hash": None, "transport_evidence": {"completed_response": False, "malformed_response": False, "provider_error": False, "synthetic": False}, "infrastructure_evidence": {"stage": "pre_provider", "reason_code": "CONTAINMENT_FAILURE", "confirmed_failure": True, "classification": "PRE_PROVIDER", "terminal_classification": "INFRASTRUCTURE_FAILURE", "provider_attempt_index": None, "prior_lifecycle_completed": False, "source_mutation_observed": False, "expected_source_hash": None, "evidence_reference": "test-infra"}, "terminal_transport_evidence": {"final_attempt_classification": "INFRASTRUCTURE_FAILURE", "process_exit_code": None, "timed_out": False, "provider_error_category": None, "provider_completed_response": False, "evidence_reference": "test-infra"}})
    pilot.validate_case_result(result, manifest, _live_authorization(manifest))


def test_preprovider_transport_booleans_and_source_revision_are_exact(manifest):
    auth = _live_authorization(manifest)
    for field in ("completed_response", "malformed_response", "provider_error"):
        result = _preprovider_result(manifest, "MODEL_INACTIVE")
        result["transport_evidence"][field] = True
        with pytest.raises(pilot.PilotError):
            pilot.validate_case_result(result, manifest, auth)
    result = _preprovider_result(manifest, "MODEL_INACTIVE")
    result["source_revision"] = "0" * 40
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, auth)


def test_overlapping_preflight_observations_are_rejected(manifest):
    auth = _live_authorization(manifest)
    for field, value in (("input_price", 0.01), ("paid_fallback_used", True)):
        result = _all_structured_preprovider_result(manifest, "MODEL_INACTIVE")
        result["route_observation"][field] = value
        with pytest.raises(pilot.PilotError):
            pilot.validate_case_result(result, manifest, auth)
    result = _all_structured_preprovider_result(manifest, "CATALOG_PREFLIGHT_FAILED")
    result["route_observation"]["active_model_status"] = "INACTIVE"
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, auth)
    result = _all_structured_preprovider_result(manifest, "MANIFEST_HASH_CHANGED")
    result["route_observation"]["provider"] = "Other Provider"
    result["provider"] = "Other Provider"
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, auth)


def test_campaign_stop_provenance_is_frozen_and_typed(manifest):
    auth = _live_authorization(manifest)
    result, prior, authority = _campaign_stop_result(manifest, "CLEANUP_FAILURE")
    result["campaign_stop_evidence"]["trigger_case_id"] = "arbitrary"
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, auth, prior, authority)
    result, prior, authority = _campaign_stop_result(manifest, "CLEANUP_FAILURE")
    result["campaign_stop_evidence"]["trigger_case_id"] = result["case_id"]
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, auth, prior, authority)
    result, prior, authority = _campaign_stop_result(manifest, "CLEANUP_FAILURE")
    result["campaign_stop_evidence"]["expected_cleanup_succeeded"] = "true"
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, auth, prior, authority)
    result, prior, authority = _campaign_stop_result(manifest, "CLEANUP_FAILURE")
    result["source_revision"] = "0" * 40
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, auth, prior, authority)
    result, prior, authority = _campaign_stop_result(manifest, "CLEANUP_FAILURE")
    result["transport_evidence"]["provider_error"] = True
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, auth, prior, authority)


def _provider_error_result(manifest, classification):
    result = _live_result(manifest)
    result["terminal_status"] = "PROVIDER_ERROR"
    result["transport_evidence"] = {"completed_response": False, "malformed_response": False, "provider_error": True, "synthetic": False}
    result["terminal_transport_evidence"] = {"final_attempt_classification": classification, "process_exit_code": None if classification == "TIMEOUT" else 1, "timed_out": classification == "TIMEOUT", "provider_error_category": "TIMEOUT" if classification == "TIMEOUT" else ("PROVIDER_UNAVAILABLE" if classification == "PROVIDER_ERROR" else "SOCKET_CLOSED"), "provider_completed_response": False, "evidence_reference": "test-provider-error"}
    return result


@pytest.mark.parametrize("classification", ["TIMEOUT", "PROVIDER_ERROR", "TRANSPORT_ERROR"])
def test_valid_provider_error_classifications_pass(manifest, classification):
    pilot.validate_case_result(_provider_error_result(manifest, classification), manifest, _live_authorization(manifest))


def test_provider_error_aggregate_completed_response_is_rejected(manifest):
    result = _provider_error_result(manifest, "PROVIDER_ERROR")
    result["transport_evidence"]["completed_response"] = True
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest))


def _infrastructure_result(manifest, stage, reason):
    result = _live_result(manifest)
    result["terminal_status"] = "INFRASTRUCTURE_ERROR"
    result["infrastructure_evidence"] = {"stage": stage, "reason_code": reason, "confirmed_failure": True, "classification": pilot.INFRASTRUCTURE_CLASSIFICATIONS[stage], "terminal_classification": "INFRASTRUCTURE_FAILURE", "provider_attempt_index": 1 if stage == "provider_transport" else None, "prior_lifecycle_completed": stage == "cleanup", "source_mutation_observed": False, "expected_source_hash": None, "evidence_reference": "test-infrastructure"}
    result["terminal_transport_evidence"] = {"final_attempt_classification": "INFRASTRUCTURE_FAILURE", "process_exit_code": None, "timed_out": False, "provider_error_category": None, "provider_completed_response": False, "evidence_reference": "test-infrastructure"}
    if stage in {"pre_provider", "workspace_pre_provider", "containment_pre_provider"}:
        result.update({"logical_model_calls": 0, "provider_process_attempts": 0, "retries": 0, "valid_directives": 0, "malformed_directive_rejections": 0, "bounded_directive_feedback_events": 0, "hypotheses_created": 0, "patch_submissions": 0, "verifier_runs": 0, "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "provider_reported_cost": 0, "public_request_hash": None, "source_hash": None})
        result["transport_evidence"] = {"completed_response": False, "malformed_response": False, "provider_error": False, "synthetic": False}
        result["route_observation"]["preflight_success"] = False
    elif stage == "provider_transport":
        result["transport_evidence"] = {"completed_response": False, "malformed_response": False, "provider_error": False, "synthetic": False}
    else:
        result["terminal_transport_evidence"].update({"provider_completed_response": True, "process_exit_code": 0})
    if stage == "pdb_runtime":
        result["policy"] = "pdb-on-uncertainty"
        result["hypotheses_created"] = 1
        result["pdb_gate_decisions"] = [{"allowed": True, "reason": "allowed"}]
        result["pdb_counts"].update({"total_gate_decisions": 1, "allowed_gate_openings": 1, "sessions_started": 1})
        result["pdb_sessions_started"] = 1
    if stage == "verifier": result["verifier_runs"] = 1
    return result


@pytest.mark.parametrize("stage,reason", [
    ("pre_provider", "CONTAINMENT_FAILURE"), ("workspace_pre_provider", "WORKSPACE_FAILURE"),
    ("containment_pre_provider", "CONTAINMENT_FAILURE"), ("provider_transport", "TRANSPORT_EVIDENCE_LOSS"),
    ("controller", "CONTROLLER_FAILURE"), ("pdb_runtime", "PDB_RUNTIME_FAILURE"),
    ("verifier", "VERIFIER_FAILURE"), ("cleanup", "CLEANUP_FAILURE"),
    ("evidence_packaging", "EVIDENCE_PACKAGING_FAILURE"),
])
def test_valid_infrastructure_stage_matrix_passes(manifest, stage, reason):
    pilot.validate_case_result(_infrastructure_result(manifest, stage, reason), manifest, _live_authorization(manifest))


def test_infrastructure_stage_classification_and_terminal_contract_are_strict(manifest):
    auth = _live_authorization(manifest)
    for mutation in ({"stage": "unknown"}, {"classification": "UNKNOWN"}, {"terminal_classification": "PROVIDER_ERROR"}):
        result = _infrastructure_result(manifest, "pre_provider", "CONTAINMENT_FAILURE")
        result["infrastructure_evidence"].update(mutation)
        with pytest.raises(pilot.PilotError):
            pilot.validate_case_result(result, manifest, auth)
    result = _infrastructure_result(manifest, "pre_provider", "CONTAINMENT_FAILURE")
    result["prompt_tokens"] = 1
    with pytest.raises(pilot.PilotError): pilot.validate_case_result(result, manifest, auth)
    result = _infrastructure_result(manifest, "pre_provider", "CONTAINMENT_FAILURE")
    result["patch_submissions"] = 1
    with pytest.raises(pilot.PilotError): pilot.validate_case_result(result, manifest, auth)
    result = _infrastructure_result(manifest, "verifier", "VERIFIER_FAILURE")
    result["verifier_runs"] = 0
    with pytest.raises(pilot.PilotError): pilot.validate_case_result(result, manifest, auth)


def test_authorization_invalid_is_derived_from_supplied_artifact(manifest):
    result = _all_structured_preprovider_result(manifest, "LIVE_AUTHORIZATION_INVALID")
    artifact = _live_authorization(manifest)
    artifact["accepted_campaign_commit"] = "invalid"
    result["preflight_failure_evidence"].update({"authorization_artifact_hash": pilot.sha256_text(pilot.canonical_json(artifact)), "authorization_validation_error": "COMMIT_INVALID"})
    pilot.validate_case_result(result, manifest, artifact)
    bad_digest = copy.deepcopy(result)
    bad_digest["preflight_failure_evidence"]["authorization_artifact_hash"] = "0" * 64
    with pytest.raises(pilot.PilotError): pilot.validate_case_result(bad_digest, manifest, artifact)
    wrong_claim = copy.deepcopy(result)
    wrong_claim["preflight_failure_evidence"]["authorization_validation_error"] = "MANIFEST_MISMATCH"
    with pytest.raises(pilot.PilotError): pilot.validate_case_result(wrong_claim, manifest, artifact)


def test_campaign_stop_source_mutation_uses_trigger_task_not_blocked_task(manifest):
    result, prior, authority = _campaign_stop_result(manifest, "SOURCE_MUTATION")
    blocked = manifest["case_order"][3]
    result.update({"case_id": blocked["case_id"], "task_id": blocked["task_id"], "policy": blocked["policy"], "order_index": blocked["order_index"]})
    blocked_entry = next(item for item in manifest["inventory"] if item["task_id"] == blocked["task_id"])
    result["campaign_stop_evidence"]["expected_source_hash"] = blocked_entry["source_sha256"]
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest), prior, authority)


def test_campaign_stop_trigger_result_hash_must_bind_exact_prior_case(manifest):
    result, prior, authority = _campaign_stop_result(manifest, "CLEANUP_FAILURE")
    result["campaign_stop_evidence"]["trigger_result_sha256"] = "0" * 64
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest), prior, authority)
    result, prior, authority = _campaign_stop_result(manifest, "CLEANUP_FAILURE")
    trigger = dict(prior[next(iter(prior))].result)
    trigger["campaign_manifest_hash"] = "0" * 64
    prior[next(iter(prior))] = {"result": trigger, "sha256": pilot.result_sha256(trigger), "validated": True}
    result["campaign_stop_evidence"]["trigger_result_sha256"] = prior[next(iter(prior))]["sha256"]
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest), prior, authority)


@pytest.mark.parametrize("reason", ["CLEANUP_FAILURE", "TRANSPORT_EVIDENCE_LOSS"])
def test_prior_case_only_stop_rejects_unrelated_authority_check(manifest, reason):
    result, prior, authority = _campaign_stop_result(manifest, reason)
    result["campaign_stop_evidence"].update({
        "trigger_case_id": None,
        "trigger_result_sha256": None,
        "pre_case_authority_check_identity": "AUTHORITY_CHECK:MANIFEST",
        "authority_check_record_sha256": "0" * 64,
    })
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest), prior, authority)


def test_campaign_stop_requires_prior_result_or_authority_record(manifest):
    result, _, _ = _campaign_stop_result(manifest, "CLEANUP_FAILURE")
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest))
    result, _, _ = _campaign_stop_result(manifest, "MANIFEST_HASH_CHANGED")
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest))


def test_campaign_stop_trigger_result_commit_binding_is_exact(manifest):
    result, prior, authority = _campaign_stop_result(manifest, "CLEANUP_FAILURE")
    trigger_id = next(iter(prior))
    trigger = dict(prior[trigger_id].result)
    trigger["campaign_commit"] = "b" * 40
    prior[trigger_id] = {"result": trigger, "sha256": pilot.result_sha256(trigger), "validated": True}
    result["campaign_stop_evidence"]["trigger_result_sha256"] = prior[trigger_id]["sha256"]
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest), prior, authority)


def test_infrastructure_controller_rejects_three_true_transport_flags(manifest):
    result = _infrastructure_result(manifest, "controller", "CONTROLLER_FAILURE")
    result["transport_evidence"] = {"completed_response": True, "malformed_response": True, "provider_error": True, "synthetic": False}
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest))


def test_infrastructure_verifier_rejects_timeout_with_completed_prior_response(manifest):
    result = _infrastructure_result(manifest, "verifier", "VERIFIER_FAILURE")
    result["terminal_transport_evidence"]["timed_out"] = True
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest))


def test_infrastructure_cleanup_rejects_contradictory_transport(manifest):
    result = _infrastructure_result(manifest, "cleanup", "CLEANUP_FAILURE")
    result["transport_evidence"]["provider_error"] = True
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest))


def test_infrastructure_failure_cannot_hide_resolved_repair(manifest):
    result = _infrastructure_result(manifest, "verifier", "VERIFIER_FAILURE")
    result["independent_verifier_result"]["outcome"] = "RESOLVED"
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest))
    result["repair_outcome"] = "RESOLVED"
    pilot.validate_case_result(result, manifest, _live_authorization(manifest))


def test_exact_catalog_fingerprint_is_shared_by_all_live_cases(manifest):
    auth = _live_authorization(manifest)
    first = _live_result(manifest)
    second = _live_result(manifest)
    second["case_id"] = manifest["case_order"][1]["case_id"]
    second["task_id"] = manifest["case_order"][1]["task_id"]
    second["policy"] = manifest["case_order"][1]["policy"]
    second["order_index"] = manifest["case_order"][1]["order_index"]
    second["route_observation"]["catalog_fingerprint"] = "d" * 64
    pilot.validate_case_result(first, manifest, auth)
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(second, manifest, auth)


def test_catalog_freeze_requires_exact_authorization_fingerprint(manifest):
    auth = _live_authorization(manifest)
    auth["expected_catalog_fingerprint"] = None
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(_live_result(manifest), manifest, auth)
    auth = _live_authorization(manifest)
    auth["catalog_binding_procedure"] = "FIRST_PREFLIGHT_CAPTURE_AND_FREEZE"
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(_live_result(manifest), manifest, auth)


def test_plain_prior_result_dictionary_with_validated_true_is_rejected(manifest):
    result, prior, authority = _campaign_stop_result(manifest, "CLEANUP_FAILURE")
    binding = prior[next(iter(prior))]
    forged = {next(iter(prior)): {"result": binding.result, "sha256": binding.sha256, "validated": True}}
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest), forged, authority)


def test_plain_authority_dictionary_with_validated_true_is_rejected(manifest):
    result, prior, authority = _campaign_stop_result(manifest, "MANIFEST_HASH_CHANGED")
    identity = next(iter(authority))
    binding = authority[identity]
    forged = {identity: {"record": binding.record, "sha256": binding.sha256, "validated": True}}
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest), prior, forged)


def test_self_consistent_forged_trigger_sha_without_capability_is_rejected(manifest):
    result, prior, authority = _campaign_stop_result(manifest, "CLEANUP_FAILURE")
    trigger_id = next(iter(prior))
    trigger = prior[trigger_id].result
    forged = {trigger_id: {"result": trigger, "sha256": pilot.result_sha256(trigger), "validated": True}}
    result["campaign_stop_evidence"]["trigger_result_sha256"] = pilot.result_sha256(trigger)
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest), forged, authority)


def test_minimal_authority_record_is_rejected(manifest):
    record = {"identity": "AUTHORITY_CHECK:MANIFEST", "reason_code": "MANIFEST_HASH_CHANGED"}
    assert pilot.validate_authority_check_record(record, manifest, "MANIFEST_HASH_CHANGED") is False
    with pytest.raises(pilot.PilotError):
        pilot.validate_authority_checks([record], manifest)


def test_caller_constructed_capability_with_wrong_issuer_is_rejected(manifest):
    with pytest.raises(pilot.PilotError):
        pilot.ValidatedCaseResultBinding(_live_result(manifest), object())
    record = {"identity": "AUTHORITY_CHECK:MANIFEST", "reason_code": "MANIFEST_HASH_CHANGED"}
    with pytest.raises(pilot.PilotError):
        pilot.ValidatedAuthorityCheckBinding(record, manifest, object())


def test_trigger_result_must_first_pass_result_validator(manifest):
    trigger = _live_result(manifest)
    trigger["source_revision"] = "0" * 40
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_results_in_order([trigger], manifest, _live_authorization(manifest))


def test_invalid_trigger_result_cannot_enter_campaign_ledger(manifest):
    trigger = _live_result(manifest)
    trigger["route_observation"]["catalog_fingerprint"] = "0" * 64
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_results_in_order([trigger], manifest, _live_authorization(manifest))


def test_authority_record_must_first_pass_typed_validator(manifest):
    record = {"identity": "AUTHORITY_CHECK:MANIFEST", "reason_code": "MANIFEST_HASH_CHANGED", "expected_manifest_hash": "0" * 64, "observed_manifest_hash": "1" * 64, "evidence_reference": "test"}
    assert pilot.validate_authority_check_record(record, manifest, "MANIFEST_HASH_CHANGED") is False
    with pytest.raises(pilot.PilotError):
        pilot.validate_authority_checks([record], manifest)


@pytest.mark.parametrize("reason,field", [
    ("MANIFEST_HASH_CHANGED", "expected_manifest_hash"),
    ("QUALIFICATION_CONTRACT_CHANGED", "expected_qualification_contract_hash"),
    ("TRACKED_SOURCE_CHANGED", "expected_source_authority_hash"),
])
def test_authority_expected_hash_must_bind_to_frozen_identity(manifest, reason, field):
    identity = {"MANIFEST_HASH_CHANGED": "AUTHORITY_CHECK:MANIFEST", "QUALIFICATION_CONTRACT_CHANGED": "AUTHORITY_CHECK:QUALIFICATION_CONTRACT", "TRACKED_SOURCE_CHANGED": "AUTHORITY_CHECK:TRACKED_SOURCE"}[reason]
    observed = {"observed_manifest_hash": "0" * 64, "observed_qualification_contract_hash": "0" * 64, "observed_source_authority_hash": "0" * 64}[field.replace("expected_", "observed_")]
    record = {"identity": identity, "reason_code": reason, field: "0" * 64, field.replace("expected_", "observed_"): observed, "evidence_reference": "test"}
    assert pilot.validate_authority_check_record(record, manifest, reason) is False


def test_authority_observed_hash_must_be_valid_and_different(manifest):
    record = {"identity": "AUTHORITY_CHECK:MANIFEST", "reason_code": "MANIFEST_HASH_CHANGED", "expected_manifest_hash": pilot.manifest_hash(manifest), "observed_manifest_hash": "not-a-hash", "evidence_reference": "test"}
    assert pilot.validate_authority_check_record(record, manifest, "MANIFEST_HASH_CHANGED") is False


def test_legitimate_validated_trigger_result_authorizes_matching_stop(manifest):
    result, prior, authority = _campaign_stop_result(manifest, "CLEANUP_FAILURE")
    pilot.validate_case_result(result, manifest, _live_authorization(manifest), prior, authority)


def test_legitimate_typed_authority_record_authorizes_matching_stop(manifest):
    result, prior, authority = _campaign_stop_result(manifest, "MANIFEST_HASH_CHANGED")
    pilot.validate_case_result(result, manifest, _live_authorization(manifest), prior, authority)


def test_reason_identity_compatibility_remains_enforced_with_capabilities(manifest):
    result, prior, authority = _campaign_stop_result(manifest, "CLEANUP_FAILURE")
    result["campaign_stop_evidence"]["trigger_case_id"] = None
    result["campaign_stop_evidence"]["trigger_result_sha256"] = None
    result["campaign_stop_evidence"]["pre_case_authority_check_identity"] = "AUTHORITY_CHECK:MANIFEST"
    result["campaign_stop_evidence"]["authority_check_record_sha256"] = "0" * 64
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest), prior, authority)


# ---- validator-owned campaign ledger adversarial coverage ----
#
# 1-13 and 16 map to the required adversarial tests: no issuer token,
# non-constructible trusted state, input/copy immutability, injection
# rejection, authority-ledger immutability, frozen-order and duplicate
# enforcement, and the two legitimate stop paths. 14/15 are the focused and
# affected regression totals verified by the suite runs.


def _validator(manifest):
    return pilot.CampaignResultValidator(manifest, _live_authorization(manifest))


def test_module_exposes_no_token_that_can_issue_validated_bindings(manifest):
    assert not hasattr(pilot, "_VALIDATION_ISSUER")
    for name in dir(pilot):
        assert "ISSUER" not in name
    with pytest.raises(pilot.PilotError):
        pilot.ValidatedCaseResultBinding(_live_result(manifest))
    with pytest.raises(pilot.PilotError):
        pilot.ValidatedAuthorityCheckBinding({}, manifest)


def test_callers_cannot_construct_trusted_result_state(manifest):
    with pytest.raises(pilot.PilotError):
        pilot.ValidatedCaseResultBinding(_live_result(manifest), object())
    with pytest.raises(pilot.PilotError):
        pilot._CaseResultLedger()
    with pytest.raises(pilot.PilotError):
        pilot._StoredCaseRecord()


def test_callers_cannot_construct_trusted_authority_state(manifest):
    record = {"identity": "AUTHORITY_CHECK:MANIFEST", "reason_code": "MANIFEST_HASH_CHANGED"}
    with pytest.raises(pilot.PilotError):
        pilot.ValidatedAuthorityCheckBinding(record, manifest, object())
    with pytest.raises(pilot.PilotError):
        pilot._AuthorityCheckLedger()
    with pytest.raises(pilot.PilotError):
        pilot._StoredAuthorityRecord()


def test_modifying_input_result_after_validation_does_not_alter_ledger(manifest):
    validator = _validator(manifest)
    result = _live_result(manifest)
    case_id = result["case_id"]
    validator.validate_result(result)
    original_sha = validator.validated_case_records[case_id].sha256
    result["case_id"] = "forged"
    result["source_revision"] = "0" * 40
    still = validator.validated_case_records[case_id]
    assert still.sha256 == original_sha
    assert still.case_id == case_id
    assert still.result["source_revision"] == manifest["authority"]["revision"]


def test_modifying_a_returned_copy_does_not_alter_ledger(manifest):
    validator = _validator(manifest)
    result = _live_result(manifest)
    case_id = result["case_id"]
    validator.validate_result(result)
    returned = validator.validated_case_records[case_id].result
    returned["campaign_manifest_hash"] = "0" * 64
    returned["case_id"] = "forged"
    pristine = validator.validated_case_records[case_id].result
    assert pristine["campaign_manifest_hash"] == pilot.manifest_hash(manifest)
    assert pristine["case_id"] == case_id
    ledger_copy = validator.case_ledger_snapshot()
    ledger_copy[case_id] = {"result": {}, "sha256": "0" * 64, "validated": True}
    pristine_again = validator.validated_case_records[case_id].result
    assert pristine_again["campaign_manifest_hash"] == pilot.manifest_hash(manifest)


def test_independently_invalid_trigger_result_cannot_be_injected(manifest):
    trigger = _live_result(manifest)
    trigger["source_revision"] = "0" * 40
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_results_in_order([trigger], manifest, _live_authorization(manifest))
    validator = _validator(manifest)
    with pytest.raises(pilot.PilotError):
        validator.validate_result(trigger)
    assert len(validator.validated_case_records) == 0
    result, prior, authority = _campaign_stop_result(manifest, "CLEANUP_FAILURE")
    forged = {next(iter(prior)): {"result": trigger, "sha256": pilot.result_sha256(trigger), "validated": True}}
    result["campaign_stop_evidence"]["trigger_result_sha256"] = pilot.result_sha256(trigger)
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest), forged, authority)


def test_forged_self_consistent_trigger_digest_cannot_be_injected(manifest):
    result, prior, authority = _campaign_stop_result(manifest, "CLEANUP_FAILURE")
    trigger_id = next(iter(prior))
    trigger = prior[trigger_id].result
    prior[trigger_id] = {"result": trigger, "sha256": pilot.result_sha256(trigger), "validated": True}
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _live_authorization(manifest), prior, authority)


def test_minimal_authority_record_cannot_enter_authority_ledger(manifest):
    record = {"identity": "AUTHORITY_CHECK:MANIFEST", "reason_code": "MANIFEST_HASH_CHANGED"}
    assert pilot.validate_authority_check_record(record, manifest, "MANIFEST_HASH_CHANGED") is False
    with pytest.raises(pilot.PilotError):
        pilot.validate_authority_checks([record], manifest)
    validator = _validator(manifest)
    with pytest.raises(pilot.PilotError):
        validator.register_authority_checks([record])
    assert len(validator.validated_authority_records) == 0


def test_authority_record_changed_after_registration_does_not_alter_stored_state(manifest):
    validator = _validator(manifest)
    identity = "AUTHORITY_CHECK:MANIFEST"
    record = {"identity": identity, "reason_code": "MANIFEST_HASH_CHANGED", "expected_manifest_hash": pilot.manifest_hash(manifest), "observed_manifest_hash": "0" * 64, "evidence_reference": "test-authority"}
    validator.register_authority_checks([record])
    stored_sha = validator.validated_authority_records[identity].sha256
    record["observed_manifest_hash"] = "f" * 64
    assert validator.validated_authority_records[identity].sha256 == stored_sha
    assert validator.validated_authority_records[identity].record["observed_manifest_hash"] == "0" * 64
    with pytest.raises(pilot.PilotError):
        validator.register_authority_checks([record])


def test_out_of_order_trigger_results_are_rejected(manifest):
    first = _live_result(manifest)
    second = _live_result(manifest)
    second["case_id"] = manifest["case_order"][1]["case_id"]
    second["task_id"] = manifest["case_order"][1]["task_id"]
    second["policy"] = manifest["case_order"][1]["policy"]
    second["order_index"] = manifest["case_order"][1]["order_index"]
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_results_in_order([second, first], manifest, _live_authorization(manifest))
    validator = _validator(manifest)
    with pytest.raises(pilot.PilotError):
        validator.validate_result(second)
    assert len(validator.validated_case_records) == 0


def test_duplicate_case_replacement_is_rejected(manifest):
    validator = _validator(manifest)
    validator.validate_result(_live_result(manifest))
    with pytest.raises(pilot.PilotError):
        validator.validate_result(_live_result(manifest))
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_results_in_order([_live_result(manifest), _live_result(manifest)], manifest, _live_authorization(manifest))
    prior = pilot.validate_case_results_in_order([_live_result(manifest)], manifest, _live_authorization(manifest))
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(_live_result(manifest), manifest, _live_authorization(manifest), prior, None)


def test_legitimate_validated_prior_case_stop_succeeds(manifest):
    result, prior, authority = _campaign_stop_result(manifest, "CLEANUP_FAILURE")
    validator = _validator(manifest)
    trigger_id = next(iter(prior))
    validator.validate_result(prior[trigger_id].result)
    validator.validate_result(result)
    assert validator.validated_case_records[trigger_id].sha256 == prior[trigger_id].sha256


def test_legitimate_typed_authority_stop_succeeds(manifest):
    validator = _validator(manifest)
    identity = "AUTHORITY_CHECK:MANIFEST"
    record = {"identity": identity, "reason_code": "MANIFEST_HASH_CHANGED", "expected_manifest_hash": pilot.manifest_hash(manifest), "observed_manifest_hash": "0" * 64, "evidence_reference": "test-authority"}
    validator.register_authority_checks([record])
    result, prior, authority = _campaign_stop_result(manifest, "MANIFEST_HASH_CHANGED")
    validator.validate_result(_live_result(manifest))
    validator.validate_result(result)


def test_ledger_validation_never_calls_provider_network_or_transport(manifest, monkeypatch):
    touched = []
    monkeypatch.setattr(pilot, "run_qualification", lambda *a, **k: touched.append("qualification"))
    monkeypatch.setattr(pilot, "live", lambda *a, **k: touched.append("live"))
    result, prior, authority = _campaign_stop_result(manifest, "CLEANUP_FAILURE")
    pilot.validate_case_result(result, manifest, _live_authorization(manifest), prior, authority)
    validator = pilot.CampaignResultValidator(manifest, _live_authorization(manifest))
    validator.validate_result(prior[next(iter(prior))].result)
    assert touched == []
