from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import quixbugs_paired_pilot as pilot


@pytest.fixture
def manifest():
    return pilot.load_manifest(pilot.MANIFEST_PATH_V2)


@pytest.fixture
def v1_manifest():
    return pilot.load_manifest(pilot.MANIFEST_PATH)


def _v2_authorization(manifest):
    return {
        "authorize_live": True,
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "qualification_contract_hash": manifest["qualification_contract_hash"],
        "accepted_campaign_commit": "a" * 40,
        "permitted_case_ids": [case["case_id"] for case in manifest["case_order"]],
        "expected_opencode_version": "1.0.0",
        "expected_catalog_fingerprint": "c" * 64,
        "subscription_route_required": True,
        "expected_billing_route": "SUBSCRIPTION",
        "expected_runtime_model_id": "opencode-go/deepseek-v4-flash",
        "subscription_entitlement_confirmed": True,
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
        "terminal_status": "UNRESOLVED", "baseline_reproduction": True, "logical_model_calls": 1,
        "provider_process_attempts": 1, "valid_directives": 1,
        "prompt_tokens": 12, "completion_tokens": 8, "reasoning_tokens": 0, "provider_reported_cost": 0.0042,
        "independent_verifier_result": {"status": "COMPLETED", "outcome": "NO_OP", "lifecycle_succeeded": True},
        "transport_evidence": {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False},
        "terminal_reason_code": "UNRESOLVED_COMPLETED",
        "terminal_transport_evidence": {"final_attempt_classification": "COMPLETED_RESPONSE", "process_exit_code": 0, "timed_out": False, "provider_error_category": None, "provider_completed_response": True, "evidence_reference": "test"},
    })
    result["route_observation"].update({
        "opencode_version": "1.0.0", "active_model_status": "ACTIVE", "variant_available": True,
        "catalog_fingerprint": "c" * 64, "runtime_model_id": "opencode-go/deepseek-v4-flash",
        "billing_route": "SUBSCRIPTION", "subscription_entitlement_confirmed": True,
        "provider_reported_cost": 0.0042, "preflight_success": True,
    })
    return result


def _block_result(manifest, reason, *, route_updates=None, evidence_updates=None):
    result = pilot.public_case_record(manifest, manifest["case_order"][0])
    result.update({
        "execution_kind": "LIVE_CASE", "campaign_commit": "a" * 40, "accepted_code_commit": "a" * 40,
        "public_request_hash": None, "source_hash": None,
        "terminal_status": "BLOCKED", "terminal_reason_code": reason,
        "terminal_transport_evidence": {"final_attempt_classification": "PRE_PROVIDER_BLOCK", "process_exit_code": None, "timed_out": False, "provider_error_category": None, "provider_completed_response": False, "evidence_reference": "test-v2-preflight"},
        "blocked_evidence": {"block_kind": "live-pre-provider", "reason_code": reason, "confirmed": True, "evidence_reference": "test-v2-preflight"},
        "transport_evidence": {"completed_response": False, "malformed_response": False, "provider_error": False, "synthetic": False},
    })
    result["route_observation"].update(route_updates or {})
    evidence = {field: None for field in pilot.ALL_PREFLIGHT_FAILURE_FIELDS}
    route = result["route_observation"]
    expected = manifest["route"]
    evidence.update({
        "failure_category": reason, "evidence_reference": "test-v2-preflight",
        "expected_provider": expected["provider"], "observed_provider": route["provider"],
        "expected_model": expected["model"], "observed_model": route["model"],
        "expected_variant": expected["variant"], "observed_variant": route["variant"],
        "expected_protocol": expected["protocol"], "observed_protocol": route["protocol"],
    })
    evidence.update(evidence_updates or {})
    relevant = pilot.PRE_PROVIDER_REASON_FIELDS_V2[reason] if reason in pilot.PRE_PROVIDER_REASON_FIELDS_V2 else pilot.PRE_PROVIDER_REASON_FIELDS[reason]
    evidence = {field: (evidence.get(field) if field in relevant or field == "failure_category" else None) for field in pilot.ALL_PREFLIGHT_FAILURE_FIELDS}
    result["preflight_failure_evidence"] = evidence
    return result


# ---- v2 manifest validation ----


def test_v2_manifest_validates_and_is_frozen(manifest):
    manifest_hash = pilot.validate_manifest(manifest)
    assert manifest["campaign_id"] == "quixbugs-paired-pilot-v2"
    assert manifest["campaign_version"] == 2
    assert manifest["freeze_status"] == "FROZEN_BEFORE_LIVE"
    assert manifest_hash == "bc3df3129f1e7d184f26de5b7b8c4953a497d463b30934aaae21865b809f3171"


def test_v2_manifest_hash_differs_from_v1(v1_manifest):
    assert pilot.manifest_hash(v1_manifest) == "5d84ea22820ca38ce80dd90a5d36e6f80160220178496950f9b45be41fae19ce"
    v2_hash = pilot.manifest_hash(pilot.load_manifest(pilot.MANIFEST_PATH_V2))
    assert v2_hash != pilot.manifest_hash(v1_manifest)


def test_v2_uses_accepted_baseline_18e067f_as_planning_baseline(manifest):
    assert manifest["accepted_baseline"] == "18e067f24c337e7215139373edc699a347cf2127"
    assert manifest["planning_baseline_commit"] == "18e067f24c337e7215139373edc699a347cf2127"
    assert manifest["campaign_commit_binding"]["planning_baseline_commit"] == "18e067f24c337e7215139373edc699a347cf2127"


def test_v2_selects_the_same_three_tasks(manifest, v1_manifest):
    assert manifest["selection"]["selected_task_ids"] == v1_manifest["selection"]["selected_task_ids"] == list(pilot.EXPECTED_SELECTED)
    assert manifest["selection"]["ranking"] == v1_manifest["selection"]["ranking"]


def test_v2_preserves_the_frozen_v1_six_case_order(manifest, v1_manifest):
    v1_pairs = [(case["task_id"], case["policy"]) for case in v1_manifest["case_order"]]
    v2_pairs = [(case["task_id"], case["policy"]) for case in manifest["case_order"]]
    assert v2_pairs == v1_pairs == list(pilot.V1_FROZEN_CASE_ORDER)
    assert [case["order_index"] for case in manifest["case_order"]] == [1, 2, 3, 4, 5, 6]


def test_v2_case_ids_are_re_stamped_with_v2_prefix(manifest):
    for case in manifest["case_order"]:
        expected_id = f"quixbugs-paired-pilot-v2:{case['task_id']}:{case['policy']}"
        assert case["case_id"] == expected_id
        assert case["case_hash"] == pilot.sha256_text(expected_id)


def test_v2_case_order_is_derivable(manifest):
    assert manifest["case_order"] == pilot.case_order_v2(list(pilot.EXPECTED_SELECTED))


def test_v2_budgets_are_identical_to_v1(manifest, v1_manifest):
    assert manifest["budgets"] == v1_manifest["budgets"] == pilot.FROZEN_BUDGETS


def test_v2_keeps_protocol_13_and_frozen_qualification_contract(manifest, v1_manifest):
    assert manifest["route"]["protocol"] == "1.3"
    assert manifest["qualification_contract"] == v1_manifest["qualification_contract"]
    assert manifest["qualification_contract_hash"] == v1_manifest["qualification_contract_hash"] == "7246d289fcc689e93d93385751cbae5fa75a3c52e3c04e001f2c977a1990c52d"


def test_v2_keeps_source_integrity_authority_and_public_private_boundary(manifest, v1_manifest):
    assert manifest["source_integrity_authority"] == v1_manifest["source_integrity_authority"]
    assert manifest["public_private_boundary"] == v1_manifest["public_private_boundary"]
    assert manifest["qualification_evidence_sha256"] == v1_manifest["qualification_evidence_sha256"]


def test_v2_route_is_the_opencode_go_subscription(manifest):
    route = manifest["route"]
    assert route["provider"] == "OpenCode Go"
    assert route["model"] == "deepseek-v4-flash"
    assert route["subscription_route"] is True
    assert route["subscription_entitlement_required"] is True
    assert route["billing_route_evidence_required"] is True
    assert route["runtime_model_id_required"] is True
    assert route["provider_reported_cost_preserved"] is True


def test_v2_replaces_zero_price_eligibility_with_subscription_contract(manifest):
    route = manifest["route"]
    assert route["require_zero_input_price"] is False
    assert route["require_zero_output_price"] is False
    contract = manifest["outcome_schema"]["preflight_failure_contract"]["subscription_route_contract"]
    assert contract["authorized_billing_route"] == "SUBSCRIPTION"
    assert contract["entitlement_evidence_required_before_contact"] is True
    assert contract["billing_route_evidence_required_before_contact"] is True
    assert contract["exact_runtime_model_id_required_before_contact"] is True


def test_v2_route_excludes_all_fallback_and_substitution_routes(manifest):
    route = manifest["route"]
    for key in ("zen_route", "free_tier_substitution", "ollama_fallback", "alternate_provider",
                "model_substitution", "metered_fallback", "paid_overage_route", "per_call_billing_fallback"):
        assert route[key] is False
    contract = manifest["outcome_schema"]["preflight_failure_contract"]["subscription_route_contract"]
    for key in ("zen_route_excluded", "free_tier_substitution_excluded", "ollama_route_excluded",
                "alternate_provider_excluded", "model_substitution_excluded", "metered_fallback_excluded",
                "paid_overage_route_excluded", "per_call_billing_fallback_excluded"):
        assert contract[key] is True


def test_v2_no_invented_catalog_identity_is_frozen(manifest):
    route = manifest["route"]
    assert route["catalog_fingerprint"] is None
    assert route["opencode_version"] is None
    assert route["invoked_in_this_task"] is False


def test_v2_stop_rules_cover_subscription_billing(manifest):
    joined = " ".join(manifest["stop_rules"])
    assert "billing-route deviation" in joined
    assert "subscription entitlement loss" in joined
    assert "model substitution" in joined
    assert "before the first provider call, the campaign blocks before that call" in joined


def test_v2_result_schema_is_version_two(manifest):
    schema = manifest["outcome_schema"]
    assert schema["schema_version"] == "quixbugs-paired-pilot-result-v2"
    assert set(schema["route_failure_reason_codes"]) == pilot.ALL_PRE_PROVIDER_REASON_CODES
    assert set(schema["preflight_failure_evidence_fields"]) == set(pilot.ALL_PREFLIGHT_FAILURE_FIELDS)
    assert set(schema["route_observation_fields"]) == set(("provider", "model", "variant", "protocol", "opencode_version", "catalog_fingerprint", "runtime_model_id", "billing_route", "subscription_entitlement_confirmed", "active_model_status", "variant_available", "input_price", "output_price", "provider_reported_cost", "paid_fallback_used", "alternate_provider_used", "ollama_used", "zen_used", "free_tier_used", "metered_fallback_used", "paid_overage_used", "per_call_billing_used", "model_substitution_observed", "preflight_success"))


def test_v2_completed_case_does_not_require_zero_pricing(manifest):
    matrix = manifest["outcome_schema"]["execution_kind_block_matrix"]["LIVE_CASE_COMPLETED"]
    assert matrix["subscription_billing_route_required"] is True
    assert matrix["subscription_entitlement_evidence_required"] is True
    assert matrix["exact_runtime_model_id_required"] is True
    assert matrix["provider_reported_cost_preserved"] is True
    assert matrix["zero_pricing_required"] is False


def test_v2_manifest_rejects_route_contract_violations(manifest):
    for key, value in (("zen_route", True), ("free_tier_substitution", True), ("ollama_fallback", True),
                       ("alternate_provider", True), ("model_substitution", True), ("metered_fallback", True),
                       ("paid_overage_route", True), ("per_call_billing_fallback", True)):
        changed = copy.deepcopy(manifest)
        changed["route"][key] = value
        with pytest.raises(pilot.PilotError):
            pilot.validate_manifest(changed)


def test_v2_manifest_rejects_zero_price_eligibility_rule(manifest):
    for key in ("require_zero_input_price", "require_zero_output_price"):
        changed = copy.deepcopy(manifest)
        changed["route"][key] = True
        with pytest.raises(pilot.PilotError):
            pilot.validate_manifest(changed)


def test_v2_manifest_rejects_wrong_route_identity(manifest):
    for field, value in (("provider", "OpenCode Zen"), ("model", "deepseek-v4-flash-free"), ("variant", "default")):
        changed = copy.deepcopy(manifest)
        changed["route"][field] = value
        with pytest.raises(pilot.PilotError):
            pilot.validate_manifest(changed)


def test_v2_manifest_rejects_campaign_identity_and_baseline_changes(manifest):
    for mutation in ({"campaign_id": "quixbugs-paired-pilot-v1"}, {"campaign_version": 1},
                     {"accepted_baseline": "0" * 40}, {"planning_baseline_commit": "0" * 40}):
        changed = copy.deepcopy(manifest)
        changed.update(mutation)
        with pytest.raises(pilot.PilotError):
            pilot.validate_manifest(changed)


def test_v2_manifest_rejects_case_order_deviation(manifest):
    changed = copy.deepcopy(manifest)
    changed["case_order"] = list(reversed(changed["case_order"]))
    with pytest.raises(pilot.PilotError):
        pilot.validate_manifest(changed)


def test_v2_manifest_rejects_task_substitution(manifest):
    changed = copy.deepcopy(manifest)
    changed["selection"]["selected_task_ids"][0] = "quixbugs-bucketsort-smoke-v1"
    with pytest.raises(pilot.PilotError):
        pilot.validate_manifest(changed)


def test_v2_manifest_derives_from_v1(manifest):
    assert manifest["derived_from"]["manifest_path"] == "research/quixbugs/PAIRED_PILOT_V1.json"
    assert manifest["derived_from"]["manifest_sha256"] == "5d84ea22820ca38ce80dd90a5d36e6f80160220178496950f9b45be41fae19ce"


# ---- v2 derivation authority (fail-closed v1 binding) ----


def test_v2_manifest_rejects_missing_derived_from(manifest):
    changed = copy.deepcopy(manifest)
    del changed["derived_from"]
    with pytest.raises(pilot.PilotError):
        pilot.validate_manifest(changed)


def test_v2_manifest_rejects_derived_from_extra_or_missing_fields(manifest):
    changed = copy.deepcopy(manifest)
    changed["derived_from"]["extra_field"] = "x"
    with pytest.raises(pilot.PilotError):
        pilot.validate_manifest(changed)
    changed = copy.deepcopy(manifest)
    del changed["derived_from"]["note"]
    with pytest.raises(pilot.PilotError):
        pilot.validate_manifest(changed)


def test_v2_manifest_rejects_wrong_v1_manifest_path(manifest):
    changed = copy.deepcopy(manifest)
    changed["derived_from"]["manifest_path"] = "research/quixbugs/PAIRED_PILOT_V2.json"
    with pytest.raises(pilot.PilotError):
        pilot.validate_manifest(changed)


def test_v2_manifest_rejects_wrong_v1_manifest_hash(manifest):
    changed = copy.deepcopy(manifest)
    changed["derived_from"]["manifest_sha256"] = "0" * 64
    with pytest.raises(pilot.PilotError):
        pilot.validate_manifest(changed)


def test_v2_manifest_rejects_missing_referenced_v1_file(manifest, monkeypatch, tmp_path):
    monkeypatch.setattr(pilot, "REPO_ROOT", tmp_path)
    with pytest.raises(pilot.PilotError):
        pilot.validate_manifest(manifest)


def test_v2_manifest_rejects_v1_content_canonical_hash_drift(manifest, monkeypatch):
    real_load = pilot.load_manifest

    def mutated_load(path):
        data = real_load(path)
        if Path(path) == pilot.MANIFEST_PATH:
            data = copy.deepcopy(data)
            data["derived_provenance_marker"] = "drifted"
        return data

    monkeypatch.setattr(pilot, "load_manifest", mutated_load)
    with pytest.raises(pilot.PilotError):
        pilot.validate_manifest(manifest)


@pytest.mark.parametrize("field,value", [("campaign_id", "quixbugs-paired-pilot-v2"), ("campaign_version", 2)])
def test_v2_manifest_rejects_wrong_referenced_v1_identity(manifest, monkeypatch, field, value):
    real_load = pilot.load_manifest

    def mutated_load(path):
        data = real_load(path)
        if Path(path) == pilot.MANIFEST_PATH:
            data = copy.deepcopy(data)
            data[field] = value
        return data

    monkeypatch.setattr(pilot, "load_manifest", mutated_load)
    with pytest.raises(pilot.PilotError):
        pilot.validate_manifest(manifest)


def test_v2_manifest_rejects_v1_retained_contract_area_drift(manifest):
    changed = copy.deepcopy(manifest)
    changed["public_private_boundary"]["gold_patch_in_public_records"] = True
    with pytest.raises(pilot.PilotError):
        pilot.validate_manifest(changed)
    changed = copy.deepcopy(manifest)
    changed["budgets"]["max_hypotheses"] += 1
    with pytest.raises(pilot.PilotError):
        pilot.validate_manifest(changed)
    changed = copy.deepcopy(manifest)
    changed["qualification_contract"]["containment_runtime_limits"]["cpu_seconds"] = 99
    changed["qualification_contract_hash"] = pilot.sha256_text(pilot.canonical_json(changed["qualification_contract"]))
    with pytest.raises(pilot.PilotError):
        pilot.validate_manifest(changed)
    changed = copy.deepcopy(manifest)
    changed["stop_rules"] = [rule for rule in changed["stop_rules"] if "No task substitution" not in rule]
    with pytest.raises(pilot.PilotError):
        pilot.validate_manifest(changed)


# ---- v2 subscription-route authorization ----


def test_v2_authorization_with_subscription_contract_validates(manifest):
    assert pilot._authorization_failure_category(manifest, _v2_authorization(manifest)) is None


def test_v2_authorization_requires_the_subscription_route(manifest):
    auth = _v2_authorization(manifest)
    del auth["subscription_route_required"]
    assert pilot._authorization_failure_category(manifest, auth) == "SUBSCRIPTION_ROUTE_REQUIRED"
    auth = _v2_authorization(manifest)
    auth["subscription_route_required"] = False
    assert pilot._authorization_failure_category(manifest, auth) == "SUBSCRIPTION_ROUTE_REQUIRED"


def test_v2_authorization_requires_subscription_billing_route_binding(manifest):
    auth = _v2_authorization(manifest)
    auth["expected_billing_route"] = "ZEN"
    assert pilot._authorization_failure_category(manifest, auth) == "BILLING_ROUTE_MISMATCH"
    auth = _v2_authorization(manifest)
    del auth["expected_billing_route"]
    assert pilot._authorization_failure_category(manifest, auth) == "BILLING_ROUTE_MISMATCH"


def test_v2_authorization_requires_exact_runtime_model_id_binding(manifest):
    auth = _v2_authorization(manifest)
    del auth["expected_runtime_model_id"]
    assert pilot._authorization_failure_category(manifest, auth) == "RUNTIME_MODEL_ID_BINDING_MISSING"
    auth = _v2_authorization(manifest)
    auth["expected_runtime_model_id"] = ""
    assert pilot._authorization_failure_category(manifest, auth) == "RUNTIME_MODEL_ID_BINDING_MISSING"


def test_v2_authorization_requires_entitlement_evidence(manifest):
    auth = _v2_authorization(manifest)
    auth["subscription_entitlement_confirmed"] = False
    assert pilot._authorization_failure_category(manifest, auth) == "ENTITLEMENT_EVIDENCE_MISSING"
    auth = _v2_authorization(manifest)
    del auth["subscription_entitlement_confirmed"]
    assert pilot._authorization_failure_category(manifest, auth) == "ENTITLEMENT_EVIDENCE_MISSING"


def test_v2_authorization_rejects_zero_price_rule_contradiction(manifest):
    auth = _v2_authorization(manifest)
    auth["zero_price_required"] = True
    assert pilot._authorization_failure_category(manifest, auth) == "ZERO_PRICING_RULE_CONTRADICTION"


def test_v2_authorization_requires_no_fallback_policy(manifest):
    auth = _v2_authorization(manifest)
    auth["no_fallback_required"] = False
    assert pilot._authorization_failure_category(manifest, auth) == "FALLBACK_POLICY_MISMATCH"


def test_v1_style_authorization_does_not_authorize_the_v2_campaign(manifest):
    v1_style = _v2_authorization(manifest)
    del v1_style["subscription_route_required"]
    del v1_style["expected_billing_route"]
    del v1_style["expected_runtime_model_id"]
    del v1_style["subscription_entitlement_confirmed"]
    v1_style["zero_price_required"] = True
    assert pilot._authorization_failure_category(manifest, v1_style) == "SUBSCRIPTION_ROUTE_REQUIRED"


def test_v2_authorization_route_identity_is_exact(manifest):
    for field in ("provider", "model", "variant", "protocol"):
        auth = _v2_authorization(manifest)
        auth[field] = "wrong"
        assert pilot._authorization_failure_category(manifest, auth) == "ROUTE_MISMATCH"


def test_v2_authorization_still_requires_version_and_catalog_binding(manifest):
    auth = _v2_authorization(manifest)
    del auth["expected_opencode_version"]
    assert pilot._authorization_failure_category(manifest, auth) == "VERSION_BINDING_MISSING"
    auth = _v2_authorization(manifest)
    del auth["expected_catalog_fingerprint"]
    assert pilot._authorization_failure_category(manifest, auth) == "CATALOG_BINDING_MISSING"


def test_v2_live_mode_still_fails_closed(manifest):
    with pytest.raises(pilot.PilotError):
        pilot.live(manifest, None)
    auth_path = REPO_ROOT / "research" / "quixbugs" / "PAIRED_PILOT_V2.json"
    with pytest.raises(pilot.PilotError):
        pilot.live(manifest, auth_path)


def test_v2_campaign_stop_identity_uses_v2_route(manifest):
    result = pilot.public_case_record(manifest, manifest["case_order"][0])
    result.update({"execution_kind": "LIVE_CASE", "campaign_commit": "a" * 40, "accepted_code_commit": "a" * 40,
                   "terminal_status": "BLOCKED", "terminal_reason_code": "CLEANUP_FAILURE",
                   "termination_reason": "CLEANUP_FAILURE", "blocked_evidence": {"block_kind": "campaign-stop", "reason_code": "CLEANUP_FAILURE", "confirmed": True, "evidence_reference": "t"},
                   "terminal_transport_evidence": {"final_attempt_classification": "CAMPAIGN_STOP", "process_exit_code": None, "timed_out": False, "provider_error_category": None, "provider_completed_response": False, "evidence_reference": "t"},
                   "transport_evidence": {"completed_response": False, "malformed_response": False, "provider_error": False, "synthetic": False}})
    evidence = {field: None for field in pilot.CAMPAIGN_STOP_EVIDENCE_FIELDS}
    trigger_case = manifest["case_order"][0]
    trigger = _live_result(manifest)
    trigger.update({"case_id": trigger_case["case_id"], "task_id": trigger_case["task_id"],
                    "policy": trigger_case["policy"], "order_index": trigger_case["order_index"],
                    "terminal_status": "INFRASTRUCTURE_ERROR", "terminal_reason_code": "INFRASTRUCTURE_FAILURE",
                    "termination_reason": "injected",
                        "infrastructure_evidence": {"stage": "cleanup", "reason_code": "CLEANUP_FAILURE", "confirmed_failure": True, "classification": "CLEANUP", "terminal_classification": "INFRASTRUCTURE_FAILURE", "provider_attempt_index": None, "prior_lifecycle_completed": True, "source_mutation_observed": False, "expected_source_hash": None, "evidence_reference": "trigger"},
                    "transport_evidence": {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False},
                    "terminal_transport_evidence": {"final_attempt_classification": "INFRASTRUCTURE_FAILURE", "process_exit_code": 0, "timed_out": False, "provider_error_category": None, "provider_completed_response": True, "evidence_reference": "trigger"}})
    prior = pilot.validate_case_results_in_order([trigger], manifest, _v2_authorization(manifest))
    evidence.update({"reason_code": "CLEANUP_FAILURE", "evidence_reference": "t", "confirmed": True,
                     "trigger_case_id": trigger_case["case_id"], "trigger_result_sha256": prior[trigger_case["case_id"]].sha256,
                     "expected_cleanup_succeeded": True, "observed_cleanup_succeeded": False})
    result["case_id"] = manifest["case_order"][1]["case_id"]
    result["task_id"] = manifest["case_order"][1]["task_id"]
    result["policy"] = manifest["case_order"][1]["policy"]
    result["order_index"] = 2
    result["campaign_stop_evidence"] = evidence
    pilot.validate_case_result(result, manifest, _v2_authorization(manifest), prior)


# ---- v2 route observation ----


def test_v2_route_observation_requires_subscription_fields(manifest):
    observation = {**manifest["route"], "opencode_version": None, "active_model_status": "NOT_RUN", "variant_available": False,
                   "catalog_fingerprint": None, "input_price": 0, "output_price": 0, "paid_fallback_used": False,
                   "alternate_provider_used": False, "ollama_used": False, "preflight_success": False}
    with pytest.raises(pilot.PilotError):
        pilot.validate_route_observation(observation, manifest["route"])


def test_v2_route_observation_rejects_unknown_billing_route(manifest):
    observation = {field: None for field in pilot.V2_ROUTE_OBSERVATION_FIELDS}
    observation.update({"provider": "OpenCode Go", "model": "deepseek-v4-flash", "variant": "max", "protocol": "1.3",
                        "opencode_version": None, "active_model_status": "NOT_RUN", "variant_available": False, "catalog_fingerprint": None,
                        "input_price": 0, "output_price": 0, "provider_reported_cost": 0, "paid_fallback_used": False,
                        "alternate_provider_used": False, "ollama_used": False, "billing_route": "BILLED", "subscription_entitlement_confirmed": False,
                        "preflight_success": False})
    with pytest.raises(pilot.PilotError):
        pilot.validate_route_observation(observation, manifest["route"])


def test_v2_route_observation_rejects_billing_route_flag_contradiction(manifest):
    observation = pilot.public_case_record(manifest, manifest["case_order"][0])["route_observation"]
    observation["billing_route"] = "SUBSCRIPTION"
    observation["zen_used"] = True
    with pytest.raises(pilot.PilotError):
        pilot.validate_route_observation(observation, manifest["route"])
    observation["billing_route"] = "ZEN"
    observation["zen_used"] = False
    with pytest.raises(pilot.PilotError):
        pilot.validate_route_observation(observation, manifest["route"])


def test_v2_successful_preflight_requires_subscription_entitlement_and_runtime_model_id(manifest):
    observation = pilot.public_case_record(manifest, manifest["case_order"][0])["route_observation"]
    observation.update({"preflight_success": True, "opencode_version": "1.0.0", "catalog_fingerprint": "c" * 64,
                        "active_model_status": "ACTIVE", "variant_available": True,
                        "billing_route": "SUBSCRIPTION", "subscription_entitlement_confirmed": True,
                        "runtime_model_id": "opencode-go/deepseek-v4-flash"})
    pilot.validate_route_observation(observation, manifest["route"])
    for mutation in ({"subscription_entitlement_confirmed": False}, {"billing_route": "UNKNOWN"}, {"runtime_model_id": None}):
        changed = dict(observation)
        changed.update(mutation)
        with pytest.raises(pilot.PilotError):
            pilot.validate_route_observation(changed, manifest["route"])


def test_v2_truthful_nonzero_pricing_is_allowed_in_observations(manifest):
    observation = pilot.public_case_record(manifest, manifest["case_order"][0])["route_observation"]
    observation.update({"preflight_success": True, "opencode_version": "1.0.0", "catalog_fingerprint": "c" * 64,
                        "active_model_status": "ACTIVE", "variant_available": True,
                        "billing_route": "SUBSCRIPTION", "subscription_entitlement_confirmed": True,
                        "runtime_model_id": "opencode-go/deepseek-v4-flash",
                        "input_price": 0.0001, "output_price": 0.0002, "provider_reported_cost": 0.42})
    pilot.validate_route_observation(observation, manifest["route"])


# ---- v2 completed live results and cost accounting ----


def test_v2_completed_live_case_validates_with_subscription_route(manifest):
    result = _live_result(manifest)
    pilot.validate_case_result(result, manifest, _v2_authorization(manifest))


def test_v2_completed_live_case_preserves_provider_reported_cost(manifest):
    # The case execution cost is the aggregate of the finite monetary costs
    # explicitly reported by the actual per-call provider responses; it
    # legitimately differs from the preflight route-observation cost.  Both
    # values are preserved truthfully and are never forced to zero by
    # subscription access; absence is never fabricated.
    auth = _v2_authorization(manifest)
    result = _live_result(manifest)
    result["provider_reported_cost"] = 0.042
    pilot.validate_case_result(result, manifest, auth)
    result = _live_result(manifest)
    result["route_observation"]["provider_reported_cost"] = 0.042
    pilot.validate_case_result(result, manifest, auth)
    result = _live_result(manifest)
    result["provider_reported_cost"] = 0.042
    result["route_observation"]["provider_reported_cost"] = 0.0042
    pilot.validate_case_result(result, manifest, auth)
    result = _live_result(manifest)
    result["provider_reported_cost"] = 0.0
    pilot.validate_case_result(result, manifest, auth)


def test_v2_completed_live_case_does_not_force_zero_cost(manifest):
    result = _live_result(manifest)
    result["provider_reported_cost"] = 0.042
    result["route_observation"]["provider_reported_cost"] = 0.042
    pilot.validate_case_result(result, manifest, _v2_authorization(manifest))


def test_v2_completed_live_case_requires_subscription_billing_route(manifest):
    auth = _v2_authorization(manifest)
    for mutation in ({"billing_route": "ZEN", "zen_used": True, "subscription_entitlement_confirmed": False, "preflight_success": False},
                     {"billing_route": "UNKNOWN", "subscription_entitlement_confirmed": False, "preflight_success": False}):
        result = _live_result(manifest)
        result["route_observation"].update(mutation)
        with pytest.raises(pilot.PilotError):
            pilot.validate_case_result(result, manifest, auth)


def test_v2_completed_live_case_binds_exact_runtime_model_id(manifest):
    auth = _v2_authorization(manifest)
    result = _live_result(manifest)
    result["route_observation"]["runtime_model_id"] = "different/model-id"
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, auth)


def test_v2_completed_live_case_binds_catalog_fingerprint(manifest):
    auth = _v2_authorization(manifest)
    result = _live_result(manifest)
    result["route_observation"]["catalog_fingerprint"] = "d" * 64
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, auth)


def test_v2_negative_cost_is_rejected(manifest):
    result = _live_result(manifest)
    result["provider_reported_cost"] = -1
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _v2_authorization(manifest))


def test_v2_preprovider_block_has_zero_cost(manifest):
    result = _block_result(manifest, "SUBSCRIPTION_ENTITLEMENT_NOT_ESTABLISHED",
                           route_updates={"billing_route": "UNKNOWN", "subscription_entitlement_confirmed": False, "preflight_success": False},
                           evidence_updates={"observed_billing_route": "UNKNOWN", "subscription_entitlement_confirmed": False})
    result["provider_reported_cost"] = 0.01
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _v2_authorization(manifest))


# ---- v2 pre-provider blocking ----


@pytest.mark.parametrize("reason,route_updates,evidence_updates", [
    ("ZEN_ROUTE_OBSERVED", {"billing_route": "ZEN", "zen_used": True, "subscription_entitlement_confirmed": False, "preflight_success": False},
     {"observed_billing_route": "ZEN", "zen_route_observed": True}),
    ("FREE_TIER_SUBSTITUTION", {"billing_route": "FREE_TIER", "free_tier_used": True, "subscription_entitlement_confirmed": False, "preflight_success": False},
     {"observed_billing_route": "FREE_TIER", "free_tier_route_observed": True}),
    ("OLLAMA_ROUTE_OBSERVED", {"billing_route": "OLLAMA", "ollama_used": True, "subscription_entitlement_confirmed": False, "preflight_success": False},
     {"observed_billing_route": "OLLAMA", "ollama_route_observed": True}),
    ("METERED_FALLBACK_REQUIRED", {"billing_route": "METERED", "metered_fallback_used": True, "subscription_entitlement_confirmed": False, "preflight_success": False},
     {"observed_billing_route": "METERED", "metered_fallback_required": True}),
    ("PAID_OVERAGE_REQUIRED", {"billing_route": "SUBSCRIPTION", "subscription_entitlement_confirmed": True, "paid_overage_used": True, "preflight_success": False},
     {"paid_overage_required": True}),
    ("PER_CALL_BILLING_FALLBACK", {"billing_route": "PER_CALL", "per_call_billing_used": True, "subscription_entitlement_confirmed": False, "preflight_success": False},
     {"observed_billing_route": "PER_CALL", "per_call_billing_fallback_required": True}),
    ("SUBSCRIPTION_ENTITLEMENT_NOT_ESTABLISHED", {"billing_route": "UNKNOWN", "subscription_entitlement_confirmed": False, "preflight_success": False},
     {"observed_billing_route": "UNKNOWN", "subscription_entitlement_confirmed": False}),
    ("RUNTIME_MODEL_ID_MISMATCH", {"billing_route": "SUBSCRIPTION", "subscription_entitlement_confirmed": True, "runtime_model_id": "other/model-id", "preflight_success": False},
     {"expected_runtime_model_id": "opencode-go/deepseek-v4-flash", "observed_runtime_model_id": "other/model-id"}),
])
def test_v2_subscription_preprovider_blocks_validate(manifest, reason, route_updates, evidence_updates):
    result = _block_result(manifest, reason, route_updates=route_updates, evidence_updates=evidence_updates)
    pilot.validate_case_result(result, manifest, _v2_authorization(manifest))


def test_v2_model_substitution_block_requires_subscription_observation(manifest):
    result = _block_result(manifest, "MODEL_SUBSTITUTION_OBSERVED",
                           route_updates={"billing_route": "SUBSCRIPTION", "subscription_entitlement_confirmed": True,
                                          "runtime_model_id": "opencode-go/deepseek-v4-flash", "model": "other-model",
                                          "model_substitution_observed": True, "preflight_success": False},
                           evidence_updates={"expected_runtime_model_id": "opencode-go/deepseek-v4-flash",
                                             "observed_runtime_model_id": "opencode-go/deepseek-v4-flash",
                                             "model_substitution_observed": True})
    result["model"] = "other-model"
    pilot.validate_case_result(result, manifest, _v2_authorization(manifest))
    result["route_observation"]["billing_route"] = "ZEN"
    result["route_observation"]["zen_used"] = True
    result["preflight_failure_evidence"]["observed_billing_route"] = "ZEN"
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _v2_authorization(manifest))


def test_v2_model_substitution_forged_runtime_identity_evidence_is_rejected(manifest):
    result = _block_result(manifest, "MODEL_SUBSTITUTION_OBSERVED",
                           route_updates={"billing_route": "SUBSCRIPTION", "subscription_entitlement_confirmed": True,
                                          "runtime_model_id": "actual/substitute-model", "model": "other-model",
                                          "model_substitution_observed": True, "preflight_success": False},
                           evidence_updates={"expected_runtime_model_id": "actual/substitute-model",
                                             "observed_runtime_model_id": "actual/substitute-model",
                                             "model_substitution_observed": True})
    result["model"] = "other-model"
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _v2_authorization(manifest))


def test_v2_model_substitution_evidence_is_authorization_bound(manifest):
    result = _block_result(manifest, "MODEL_SUBSTITUTION_OBSERVED",
                           route_updates={"billing_route": "SUBSCRIPTION", "subscription_entitlement_confirmed": True,
                                          "runtime_model_id": "actual/substitute-model", "model": "other-model",
                                          "model_substitution_observed": True, "preflight_success": False},
                           evidence_updates={"expected_runtime_model_id": "opencode-go/deepseek-v4-flash",
                                             "observed_runtime_model_id": "actual/substitute-model",
                                             "model_substitution_observed": True})
    result["model"] = "other-model"
    pilot.validate_case_result(result, manifest, _v2_authorization(manifest))
    result["preflight_failure_evidence"]["expected_runtime_model_id"] = "actual/substitute-model"
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _v2_authorization(manifest))


def test_v2_entitlement_block_is_not_derived_for_specific_observed_route(manifest):
    result = _block_result(manifest, "SUBSCRIPTION_ENTITLEMENT_NOT_ESTABLISHED",
                           route_updates={"billing_route": "ZEN", "zen_used": True, "subscription_entitlement_confirmed": False, "preflight_success": False},
                           evidence_updates={"observed_billing_route": "ZEN", "zen_route_observed": True,
                                             "subscription_entitlement_confirmed": False})
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _v2_authorization(manifest))


def test_v2_preprovider_block_reason_mismatch_is_rejected(manifest):
    result = _block_result(manifest, "ZEN_ROUTE_OBSERVED",
                           route_updates={"billing_route": "ZEN", "zen_used": True, "subscription_entitlement_confirmed": False, "preflight_success": False},
                           evidence_updates={"observed_billing_route": "ZEN", "zen_route_observed": True})
    result["terminal_reason_code"] = "FREE_TIER_SUBSTITUTION"
    result["blocked_evidence"]["reason_code"] = "FREE_TIER_SUBSTITUTION"
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _v2_authorization(manifest))


def test_v2_preprovider_block_has_no_case_activity(manifest):
    result = _block_result(manifest, "ZEN_ROUTE_OBSERVED",
                           route_updates={"billing_route": "ZEN", "zen_used": True, "subscription_entitlement_confirmed": False, "preflight_success": False},
                           evidence_updates={"observed_billing_route": "ZEN", "zen_route_observed": True})
    result["logical_model_calls"] = 1
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _v2_authorization(manifest))


def test_v2_legacy_preprovider_reasons_still_work_on_v2_route(manifest):
    result = _block_result(manifest, "MODEL_INACTIVE",
                           route_updates={"billing_route": "SUBSCRIPTION", "subscription_entitlement_confirmed": True,
                                          "active_model_status": "INACTIVE", "preflight_success": False},
                           evidence_updates={"observed_active_model_status": "INACTIVE"})
    pilot.validate_case_result(result, manifest, _v2_authorization(manifest))
    result = _block_result(manifest, "VARIANT_UNAVAILABLE",
                           route_updates={"billing_route": "SUBSCRIPTION", "subscription_entitlement_confirmed": True,
                                          "active_model_status": "ACTIVE", "variant_available": False, "preflight_success": False},
                           evidence_updates={"observed_active_model_status": "ACTIVE", "observed_variant_available": False})
    pilot.validate_case_result(result, manifest, _v2_authorization(manifest))
    result = _block_result(manifest, "PROVIDER_MISMATCH",
                           route_updates={"provider": "OpenCode Zen", "billing_route": "SUBSCRIPTION", "subscription_entitlement_confirmed": True, "preflight_success": False})
    result["provider"] = "OpenCode Zen"
    pilot.validate_case_result(result, manifest, _v2_authorization(manifest))


def test_v2_alternate_provider_block_is_enforced(manifest):
    result = _block_result(manifest, "ALTERNATE_PROVIDER_REQUIRED",
                           route_updates={"billing_route": "SUBSCRIPTION", "subscription_entitlement_confirmed": True,
                                          "active_model_status": "ACTIVE", "variant_available": True,
                                          "alternate_provider_used": True, "preflight_success": False},
                           evidence_updates={"alternate_provider_required": True})
    pilot.validate_case_result(result, manifest, _v2_authorization(manifest))


def test_v2_paid_fallback_block_is_enforced(manifest):
    result = _block_result(manifest, "PAID_FALLBACK_REQUIRED",
                           route_updates={"billing_route": "SUBSCRIPTION", "subscription_entitlement_confirmed": True,
                                          "active_model_status": "ACTIVE", "variant_available": True,
                                          "paid_fallback_used": True, "preflight_success": False},
                           evidence_updates={"paid_fallback_required": True})
    pilot.validate_case_result(result, manifest, _v2_authorization(manifest))


def test_v2_completed_case_rejects_fallback_usage(manifest):
    result = _live_result(manifest)
    result["route_observation"]["paid_overage_used"] = True
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _v2_authorization(manifest))
    result = _live_result(manifest)
    result["route_observation"]["model_substitution_observed"] = True
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _v2_authorization(manifest))


def test_v2_contradictory_preflight_evidence_is_rejected(manifest):
    result = _block_result(manifest, "SUBSCRIPTION_ENTITLEMENT_NOT_ESTABLISHED",
                           route_updates={"billing_route": "UNKNOWN", "subscription_entitlement_confirmed": False, "preflight_success": False},
                           evidence_updates={"observed_billing_route": "UNKNOWN", "subscription_entitlement_confirmed": False})
    result["preflight_failure_evidence"]["zen_route_observed"] = True
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _v2_authorization(manifest))


def test_v2_missing_preflight_evidence_fields_are_rejected(manifest):
    result = _block_result(manifest, "ZEN_ROUTE_OBSERVED",
                           route_updates={"billing_route": "ZEN", "zen_used": True, "subscription_entitlement_confirmed": False, "preflight_success": False},
                           evidence_updates={"observed_billing_route": "ZEN", "zen_route_observed": True})
    del result["preflight_failure_evidence"]["observed_billing_route"]
    with pytest.raises(pilot.PilotError):
        pilot.validate_case_result(result, manifest, _v2_authorization(manifest))


# ---- v2 dry-run and validation-only defaults ----


def test_v2_dry_run_starts_no_provider_process_and_walks_all_cases(manifest):
    evidence = pilot.dry_run(manifest, fail_at=None)
    assert evidence["provider_processes_started"] == 0
    assert evidence["network_activity"] is False
    assert evidence["all_six_walked"] is True
    assert evidence["fresh_case_resources"] is True


def test_v2_default_mode_is_validation_only(manifest, monkeypatch):
    called = []
    monkeypatch.setattr(pilot, "run_qualification", lambda value: called.append(value))
    assert pilot.main(["--manifest", str(pilot.MANIFEST_PATH_V2)]) == 0
    assert called == []


def test_v2_plan_reports_six_cases_without_provider_contact(manifest):
    plan = pilot.plan(manifest)
    assert len(plan["cases"]) == 6
    assert plan["provider_contacted"] is False


def test_v2_public_record_carries_subscription_observation_fields(manifest):
    result = pilot.public_case_record(manifest, manifest["case_order"][0])
    assert result["route_observation"]["billing_route"] == "UNKNOWN"
    assert result["route_observation"]["subscription_entitlement_confirmed"] is False
    assert result["route_observation"]["runtime_model_id"] is None
    assert result["route_observation"]["provider_reported_cost"] == 0


# ---- v1 compatibility ----


def test_v1_manifest_and_helpers_are_unchanged(v1_manifest):
    assert pilot.validate_manifest(v1_manifest) == "5d84ea22820ca38ce80dd90a5d36e6f80160220178496950f9b45be41fae19ce"
    assert pilot.case_order(list(pilot.EXPECTED_SELECTED)) == v1_manifest["case_order"]
    assert pilot.selection_ranking(v1_manifest["inventory"]) == v1_manifest["selection"]["ranking"]


def test_v2_ranking_uses_the_frozen_v1_selection_salt(manifest, v1_manifest):
    assert pilot.selection_ranking(manifest["inventory"], campaign_id=pilot.CAMPAIGN_ID) == v1_manifest["selection"]["ranking"]
    assert manifest["selection"]["ranking"] == v1_manifest["selection"]["ranking"]


def test_v1_live_results_remain_valid_with_zero_pricing(v1_manifest):
    auth = {
        "authorize_live": True,
        "campaign_manifest_hash": pilot.manifest_hash(v1_manifest),
        "qualification_contract_hash": v1_manifest["qualification_contract_hash"],
        "accepted_campaign_commit": "a" * 40,
        "permitted_case_ids": [case["case_id"] for case in v1_manifest["case_order"]],
        "expected_opencode_version": "1.0.0",
        "expected_catalog_fingerprint": "c" * 64,
        "zero_price_required": True,
        "no_fallback_required": True,
        **{key: v1_manifest["route"][key] for key in ("provider", "model", "variant", "protocol")},
    }
    result = pilot.public_case_record(v1_manifest, v1_manifest["case_order"][0])
    result.update({
        "execution_kind": "LIVE_CASE", "campaign_commit": "a" * 40, "accepted_code_commit": "a" * 40,
        "public_request_hash": "b" * 64,
        "source_hash": next(item["source_sha256"] for item in v1_manifest["inventory"] if item["task_id"] == result["task_id"]),
        "terminal_status": "UNRESOLVED", "baseline_reproduction": True, "logical_model_calls": 1,
        "provider_process_attempts": 1, "valid_directives": 1,
        "independent_verifier_result": {"status": "COMPLETED", "outcome": "NO_OP", "lifecycle_succeeded": True},
        "transport_evidence": {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False},
        "terminal_reason_code": "UNRESOLVED_COMPLETED",
        "terminal_transport_evidence": {"final_attempt_classification": "COMPLETED_RESPONSE", "process_exit_code": 0, "timed_out": False, "provider_error_category": None, "provider_completed_response": True, "evidence_reference": "test"},
    })
    result["route_observation"].update({"opencode_version": "1.0.0", "active_model_status": "ACTIVE", "variant_available": True, "catalog_fingerprint": "c" * 64, "preflight_success": True})
    pilot.validate_case_result(result, v1_manifest, auth)
    assert result["provider_reported_cost"] == 0
    assert result["route_observation"]["input_price"] == 0


def test_v2_case_ids_do_not_collide_with_v1(manifest, v1_manifest):
    assert {case["case_id"] for case in manifest["case_order"]}.isdisjoint({case["case_id"] for case in v1_manifest["case_order"]})


def test_v1_route_observation_validation_is_unchanged(v1_manifest):
    observation = pilot.public_case_record(v1_manifest, v1_manifest["case_order"][0])["route_observation"]
    observation.update({"opencode_version": "1.0.0", "active_model_status": "ACTIVE", "variant_available": True,
                        "catalog_fingerprint": "c" * 64, "input_price": 0, "output_price": 0,
                        "paid_fallback_used": False, "alternate_provider_used": False, "ollama_used": False,
                        "preflight_success": True})
    pilot.validate_route_observation(observation, v1_manifest["route"])
    observation["input_price"] = 0.01
    with pytest.raises(pilot.PilotError):
        pilot.validate_route_observation(observation, v1_manifest["route"])


def test_validator_entrypoint_validates_both_versions():
    from scripts.validate_quixbugs_paired_pilot import TRACKED_MANIFESTS
    assert len(TRACKED_MANIFESTS) == 4
    for path in TRACKED_MANIFESTS:
        pilot.validate_manifest(pilot.load_manifest(path))
