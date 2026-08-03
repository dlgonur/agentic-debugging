"""Strict adapter-configuration contract tests for the OpenCode Go execution adapter."""
from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

import quixbugs_opencode_go_adapter as adapter
import quixbugs_live_runner_v2 as runner
import quixbugs_paired_pilot as pilot

from opencode_go_test_support import wrapper_command, wrapper_environment_allowlist


@pytest.fixture
def manifest():
    return pilot.load_manifest(pilot.MANIFEST_PATH_V2)


@pytest.fixture
def synthetic_executable() -> Path:
    return REPO_ROOT / "scripts" / "opencode_go_synthetic_executable.py"


@pytest.fixture
def boundary(tmp_path: Path) -> Path:
    return adapter.common_operator_boundary([sys.executable, tmp_path])


def _valid_configuration(manifest, tmp_path, synthetic_executable, **overrides) -> dict:
    interpreter = sys.executable
    runtime_model_id = "opencode-go/test-deepseek-v4-flash"
    boundary = adapter.common_operator_boundary([interpreter, synthetic_executable, tmp_path])
    value = {
        "schema_version": adapter.ADAPTER_SCHEMA_VERSION,
        "template": False,
        "adapter_identity": adapter.ADAPTER_IDENTITY,
        "campaign_id": "quixbugs-paired-pilot-v2",
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "operator_authorization_id": "test-operator-001",
        "authorization_hash": "a" * 64,
        "execution_commit": runner.ACCEPTED_BASELINE,
        "executable": interpreter,
        "command": wrapper_command(interpreter, runtime_model_id),
        "working_directory": str(tmp_path),
        "operator_boundary_root": str(boundary),
        "protocol_version": "1.3",
        "provider": "OpenCode Go",
        "model_family": "deepseek-v4-flash",
        "variant": "max",
        "runtime_model_id": runtime_model_id,
        "opencode_version": "1.0.0",
        "catalog_fingerprint": "c" * 64,
        "route_class": "SUBSCRIPTION",
        "expected_account_status": "ACTIVE",
        "per_call_timeout_seconds": 30.0,
        "total_case_timeout_seconds": 60.0,
        "environment_allowlist": wrapper_environment_allowlist(),
        "max_stdout_bytes": 262144,
        "max_stderr_bytes": 262144,
        "max_diagnostic_bytes": 16384,
        "transport_retry_limit": 0,
        "max_transport_attempts_per_logical_call": 1,
        "no_automatic_route_discovery": True,
        "no_global_model_selection": True,
        "requires_active_authorization_binding": True,
        "deny_zen_route": True,
        "deny_free_tier_substitution": True,
        "deny_ollama_route": True,
        "deny_alternate_provider": True,
        "deny_model_substitution": True,
        "deny_metered_fallback": True,
        "deny_paid_overage": True,
        "deny_per_call_billing_fallback": True,
        "no_fallback_required": True,
    }
    value.update(overrides)
    return value


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _valid_authorization(manifest, tmp_path, **overrides) -> dict:
    value = {
        "schema_version": runner.AUTHORIZATION_SCHEMA_VERSION,
        "template": False,
        "authorize_live": True,
        "campaign_id": "quixbugs-paired-pilot-v2",
        "campaign_version": 2,
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "accepted_baseline": runner.ACCEPTED_BASELINE,
        "planning_baseline_commit": manifest["planning_baseline_commit"],
        "qualification_contract_hash": manifest["qualification_contract_hash"],
        "accepted_campaign_commit": runner.ACCEPTED_BASELINE,
        "permitted_case_ids": [case["case_id"] for case in manifest["case_order"]],
        "provider": "OpenCode Go",
        "model": "deepseek-v4-flash",
        "variant": "max",
        "protocol": "1.3",
        "expected_opencode_version": "1.0.0",
        "expected_catalog_fingerprint": "c" * 64,
        "expected_runtime_model_id": "opencode-go/test-deepseek-v4-flash",
        "subscription_route_required": True,
        "expected_billing_route": "SUBSCRIPTION",
        "subscription_entitlement_confirmed": True,
        "subscription_account_observation": {"entitlement_confirmed": True, "evidence_reference": "test-account-001"},
        "expected_account_status": "ACTIVE",
        "billing_route_classification": "SUBSCRIPTION",
        "deny_zen_route": True,
        "deny_free_tier_substitution": True,
        "deny_ollama_route": True,
        "deny_alternate_provider": True,
        "deny_model_substitution": True,
        "deny_metered_fallback": True,
        "deny_paid_overage": True,
        "deny_per_call_billing_fallback": True,
        "no_fallback_required": True,
        "operator_authorization_id": "test-operator-001",
        "authorization_created_at": "2026-08-02T00:00:00Z",
        "authorization_valid_until": None,
        "output_root": str(tmp_path / "attempt-out"),
        "campaign_attempt_identity": "quixbugs-paired-pilot-v2-attempt-" + "d" * 64,
        "single_frozen_six_case_campaign_confirmation": True,
    }
    value.update(overrides)
    return value


def _route_evidence(manifest, **overrides) -> dict:
    value = {
        "provider": "OpenCode Go",
        "model": "deepseek-v4-flash",
        "variant": "max",
        "protocol": "1.3",
        "opencode_version": "1.0.0",
        "catalog_fingerprint": "c" * 64,
        "runtime_model_id": "opencode-go/test-deepseek-v4-flash",
        "billing_route": "SUBSCRIPTION",
        "subscription_entitlement_confirmed": True,
        "account_status": "ACTIVE",
        "active_model_status": "ACTIVE",
        "variant_available": True,
        "input_price": 0.1,
        "output_price": 0.2,
        "provider_reported_cost": 0.0042,
        "paid_fallback_used": False,
        "alternate_provider_used": False,
        "ollama_used": False,
        "zen_used": False,
        "free_tier_used": False,
        "metered_fallback_used": False,
        "paid_overage_used": False,
        "per_call_billing_used": False,
        "model_substitution_observed": False,
        "observed_at": _now_iso(),
    }
    value.update(overrides)
    return value


def _observed(manifest, **overrides) -> dict:
    value = _route_evidence(manifest, **overrides)
    value.update({"preflight_success": True, "execution_commit": runner.ACCEPTED_BASELINE})
    return value


# ---- structural validation ---------------------------------------------------


def test_valid_configuration_passes_structure(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable)
    validated = adapter.validate_adapter_configuration_structure(value)
    assert validated["runtime_model_id"] == "opencode-go/test-deepseek-v4-flash"
    assert validated["route_class"] == "SUBSCRIPTION"


def test_template_is_rejected_as_active_configuration(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable, template=True)
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "TEMPLATE_IS_NOT_CONFIGURATION"


def test_tracked_template_file_fails_as_active_configuration():
    path = REPO_ROOT / "research" / "quixbugs" / "OPENCODE_GO_EXECUTION_ADAPTER_TEMPLATE.json"
    assert path.is_file()
    value = adapter.load_adapter_configuration(path)
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "TEMPLATE_IS_NOT_CONFIGURATION"


def test_unknown_fields_rejected(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable, surprise_field=True)
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "UNKNOWN_FIELDS"


def test_missing_fields_rejected(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable)
    del value["runtime_model_id"]
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "MISSING_FIELDS"


def test_wrong_type_rejected(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable, variant=42)
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "WRONG_TYPE"


def test_string_shell_command_rejected(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable)
    value["command"] = f"{sys.executable} run --pure"
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "STRING_SHELL_COMMAND"


def test_empty_argv_element_rejected(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable)
    value["command"] = [sys.executable, "", "--model", "x", "--variant", "max"]
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "EMPTY_ARGV_ELEMENT"


def test_shell_metacharacter_rejected(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable)
    value["command"] = [sys.executable, str(synthetic_executable), ";", "rm", "-rf", "/"]
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "SHELL_METACHARACTER"


def test_direct_opencode_cli_bypass_rejected(manifest, tmp_path, synthetic_executable):
    """A direct ``opencode run ...`` command that bypasses the accepted
    protocol wrapper is rejected."""
    fake_cli = tmp_path / "opencode.cmd"
    fake_cli.write_text("@echo off\n", encoding="utf-8")
    value = _valid_configuration(manifest, tmp_path, synthetic_executable)
    value["executable"] = str(fake_cli)
    value["command"] = [
        str(fake_cli), "run", "message", "--pure", "--format", "json",
        "--model", "opencode-go/test-deepseek-v4-flash", "--variant", "max",
        "--dir", str(tmp_path), "--file", str(tmp_path / "request.json"),
    ]
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "DIRECT_OPENCODE_COMMAND_REJECTED"


def test_wrapper_not_bound_rejected(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable)
    other_script = tmp_path / "not-the-wrapper.py"
    other_script.write_text("raise SystemExit(0)\n", encoding="utf-8")
    value["command"] = [sys.executable, str(other_script), "--model", "m", "--variant", "v"]
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "WRAPPER_NOT_BOUND"


def test_route_mode_not_bound_rejected(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable)
    command = list(value["command"])
    command[command.index("--route-mode") + 1] = "legacy"
    value["command"] = command
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "ROUTE_MODE_NOT_BOUND"


def test_route_binding_flags_missing_rejected(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable)
    command = list(value["command"])
    del command[command.index("--expected-catalog-fingerprint"):command.index("--expected-catalog-fingerprint") + 2]
    value["command"] = command
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "ROUTE_BINDING_FLAGS_MISSING"


def test_command_binds_exact_wrapper_and_identity(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable)
    validated = adapter.validate_adapter_configuration_structure(value)
    command = validated["command"]
    assert Path(command[1]).resolve() == (REPO_ROOT / "scripts" / "opencode_protocol_transport.py").resolve()
    pairs = {(command[i], command[i + 1]) for i in range(len(command) - 1)}
    assert ("--model", "opencode-go/test-deepseek-v4-flash") in pairs
    assert ("--variant", "max") in pairs
    assert ("--route-mode", "opencode-go") in pairs
    assert ("--expected-opencode-version", "1.0.0") in pairs
    assert ("--expected-catalog-fingerprint", "c" * 64) in pairs
    assert ("--expected-runtime-model-id", "opencode-go/test-deepseek-v4-flash") in pairs
    assert ("--expected-account-status", "ACTIVE") in pairs
    assert ("--expected-billing-route", "SUBSCRIPTION") in pairs


def test_relative_executable_rejected(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable)
    value["executable"] = "python.exe"
    value["command"] = ["python.exe", str(synthetic_executable), "--model", "m", "--variant", "v"]
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "RELATIVE_EXECUTABLE"


def test_executable_not_first_argv_rejected(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable)
    value["command"] = [sys.executable, str(synthetic_executable), "--model", "m", "--variant", "v"]
    value["executable"] = str(synthetic_executable)
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "EXECUTABLE_NOT_FIRST_ARGV"


def test_executable_outside_operator_boundary_rejected(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable)
    value["operator_boundary_root"] = str(tmp_path / "nested" / "boundary")
    (tmp_path / "nested" / "boundary").mkdir(parents=True, exist_ok=True)
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "EXECUTABLE_OUTSIDE_BOUNDARY"


def test_working_directory_outside_operator_boundary_rejected(tmp_path):
    fixture_exe = tmp_path / "fake-executable.py"
    fixture_exe.write_text("raise SystemExit(0)\n", encoding="utf-8")
    outside = tmp_path / ".." / f"outside-{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    manifest = pilot.load_manifest(pilot.MANIFEST_PATH_V2)
    value = _valid_configuration(manifest, tmp_path, fixture_exe)
    value["executable"] = str(fixture_exe)
    value["command"] = [str(fixture_exe), "--model", "m", "--variant", "v"]
    value["operator_boundary_root"] = str(tmp_path)
    value["working_directory"] = str(outside)
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "WORKING_DIRECTORY_OUTSIDE_BOUNDARY"


def test_credential_in_argv_rejected(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable)
    value["command"] = [sys.executable, str(synthetic_executable), "--api-key", "super-secret"]
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "CREDENTIAL_IN_CONFIGURATION"


def test_historical_zen_identity_rejected(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(
        manifest, tmp_path, synthetic_executable,
        runtime_model_id=adapter.HISTORICAL_ZEN_MODEL_ID,
    )
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "HISTORICAL_ZEN_IDENTITY"


def test_denial_flag_not_true_rejected(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable, deny_zen_route=False)
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "DENIAL_FLAG_NOT_TRUE"


def test_no_fallback_required_rejected(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable, no_fallback_required=False)
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "FALLBACK_POLICY_MISMATCH"


def test_automatic_route_discovery_not_denied(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable, no_automatic_route_discovery=False)
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "AUTOMATIC_ROUTE_DISCOVERY_NOT_DENIED"


def test_secret_named_allowlist_entry_rejected(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable)
    value["environment_allowlist"] = ["PATH", "OPENAI_API_KEY"]
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "ALLOWLIST_SECRET_NAME"


def test_timeout_contradiction_rejected(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable, per_call_timeout_seconds=100.0, total_case_timeout_seconds=50.0)
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "TIMEOUT_CONTRADICTION"


def test_transport_accounting_contradiction_rejected(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable, transport_retry_limit=3, max_transport_attempts_per_logical_call=3)
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "TRANSPORT_ACCOUNTING_CONTRADICTION"


def test_non_finite_value_rejected(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable, per_call_timeout_seconds=float("inf"))
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "NON_FINITE_VALUE"


def test_wrong_protocol_rejected(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable, protocol_version="1.2")
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "PROTOCOL_MISMATCH"


def test_wrong_route_class_rejected(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable, route_class="ZEN")
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "ROUTE_CLASS_MISMATCH"


def test_wrong_schema_version_rejected(manifest, tmp_path, synthetic_executable):
    value = _valid_configuration(manifest, tmp_path, synthetic_executable, schema_version="quixbugs-opencode-go-execution-adapter-v0")
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "SCHEMA_VERSION_MISMATCH"


# ---- binding validation ------------------------------------------------------


def test_binding_agreement_passes(manifest, tmp_path, synthetic_executable):
    configuration = _valid_configuration(manifest, tmp_path, synthetic_executable)
    authorization = _valid_authorization(manifest, tmp_path)
    observed = _observed(manifest)
    validated = adapter.validate_adapter_configuration_structure(configuration)
    validated["authorization_hash"] = runner.authorization_hash(authorization)
    bound = adapter.bind_adapter_configuration(validated, manifest, authorization, observed)
    binding = adapter.build_runtime_identity_binding(authorization, observed, bound)
    assert binding.runtime_model_id == "opencode-go/test-deepseek-v4-flash"
    assert binding.route_class == "SUBSCRIPTION"
    assert binding.fingerprint()


def test_binding_authorization_hash_mismatch(manifest, tmp_path, synthetic_executable):
    configuration = _valid_configuration(manifest, tmp_path, synthetic_executable)
    authorization = _valid_authorization(manifest, tmp_path)
    observed = _observed(manifest)
    validated = adapter.validate_adapter_configuration_structure(configuration)
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.bind_adapter_configuration(validated, manifest, authorization, observed)
    assert exc.value.reason == "AUTHORIZATION_HASH_MISMATCH"


def test_binding_commit_mismatch(manifest, tmp_path, synthetic_executable):
    configuration = _valid_configuration(manifest, tmp_path, synthetic_executable)
    authorization = _valid_authorization(manifest, tmp_path)
    observed = _observed(manifest)
    validated = adapter.validate_adapter_configuration_structure(configuration)
    authorization["accepted_campaign_commit"] = "b" * 40
    validated["authorization_hash"] = runner.authorization_hash(authorization)
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.bind_adapter_configuration(validated, manifest, authorization, observed)
    assert exc.value.reason == "COMMIT_MISMATCH"


def test_binding_runtime_model_id_mismatch(manifest, tmp_path, synthetic_executable):
    configuration = _valid_configuration(manifest, tmp_path, synthetic_executable)
    authorization = _valid_authorization(manifest, tmp_path)
    observed = _observed(manifest)
    validated = adapter.validate_adapter_configuration_structure(configuration)
    validated["authorization_hash"] = runner.authorization_hash(authorization)
    observed["runtime_model_id"] = "opencode-go/different-model"
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.bind_adapter_configuration(validated, manifest, authorization, observed)
    assert exc.value.reason == "RUNTIME_MODEL_ID_MISMATCH"


def test_binding_route_observation_not_established(manifest, tmp_path, synthetic_executable):
    configuration = _valid_configuration(manifest, tmp_path, synthetic_executable)
    authorization = _valid_authorization(manifest, tmp_path)
    observed = _observed(manifest)
    observed["preflight_success"] = False
    validated = adapter.validate_adapter_configuration_structure(configuration)
    validated["authorization_hash"] = runner.authorization_hash(authorization)
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.bind_adapter_configuration(validated, manifest, authorization, observed)
    assert exc.value.reason == "ROUTE_OBSERVATION_NOT_ESTABLISHED"


def test_binding_budget_contradiction(manifest, tmp_path, synthetic_executable):
    configuration = _valid_configuration(manifest, tmp_path, synthetic_executable, total_case_timeout_seconds=2000.0)
    authorization = _valid_authorization(manifest, tmp_path)
    observed = _observed(manifest)
    validated = adapter.validate_adapter_configuration_structure(configuration)
    validated["authorization_hash"] = runner.authorization_hash(authorization)
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.bind_adapter_configuration(validated, manifest, authorization, observed)
    assert exc.value.reason == "BUDGET_CONTRADICTION"


def test_command_missing_model_binding_rejected(manifest, tmp_path, synthetic_executable):
    """A wrapper command that omits the --model binding is rejected
    structurally."""
    configuration = _valid_configuration(manifest, tmp_path, synthetic_executable)
    command = list(configuration["command"])
    del command[command.index("--model"):command.index("--model") + 2]
    configuration["command"] = command
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(configuration)
    assert exc.value.reason == "ROUTE_BINDING_FLAGS_MISSING"


# ---- runtime identity binding ------------------------------------------------


def test_runtime_identity_rejects_zen_route(manifest, tmp_path, synthetic_executable):
    configuration = _valid_configuration(manifest, tmp_path, synthetic_executable)
    authorization = _valid_authorization(manifest, tmp_path)
    observed = _observed(manifest, billing_route="ZEN")
    validated = adapter.validate_adapter_configuration_structure(configuration)
    validated["authorization_hash"] = runner.authorization_hash(authorization)
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.build_runtime_identity_binding(authorization, observed, validated)
    assert exc.value.reason == "BILLING_ROUTE_MISMATCH"


def test_runtime_identity_rejects_free_tier_observed(manifest, tmp_path, synthetic_executable):
    configuration = _valid_configuration(manifest, tmp_path, synthetic_executable)
    authorization = _valid_authorization(manifest, tmp_path)
    observed = _observed(manifest, free_tier_used=True)
    validated = adapter.validate_adapter_configuration_structure(configuration)
    validated["authorization_hash"] = runner.authorization_hash(authorization)
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.build_runtime_identity_binding(authorization, observed, validated)
    assert exc.value.reason == "FREE_TIER_SUBSTITUTION"


def test_runtime_identity_rejects_model_substitution_observed(manifest, tmp_path, synthetic_executable):
    configuration = _valid_configuration(manifest, tmp_path, synthetic_executable)
    authorization = _valid_authorization(manifest, tmp_path)
    observed = _observed(manifest, model_substitution_observed=True)
    validated = adapter.validate_adapter_configuration_structure(configuration)
    validated["authorization_hash"] = runner.authorization_hash(authorization)
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.build_runtime_identity_binding(authorization, observed, validated)
    assert exc.value.reason == "MODEL_SUBSTITUTION_OBSERVED"


def test_runtime_identity_rejects_historical_zen_execution_identity(manifest, tmp_path, synthetic_executable):
    configuration = _valid_configuration(manifest, tmp_path, synthetic_executable)
    authorization = _valid_authorization(manifest, tmp_path)
    observed = _observed(manifest, runtime_model_id=adapter.HISTORICAL_ZEN_MODEL_ID)
    validated = adapter.validate_adapter_configuration_structure(configuration)
    validated["authorization_hash"] = runner.authorization_hash(authorization)
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.build_runtime_identity_binding(authorization, observed, validated)
    assert exc.value.reason == "HISTORICAL_ZEN_IDENTITY"


def test_runtime_identity_rejects_ollama_observed(manifest, tmp_path, synthetic_executable):
    configuration = _valid_configuration(manifest, tmp_path, synthetic_executable)
    authorization = _valid_authorization(manifest, tmp_path)
    observed = _observed(manifest, ollama_used=True)
    validated = adapter.validate_adapter_configuration_structure(configuration)
    validated["authorization_hash"] = runner.authorization_hash(authorization)
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.build_runtime_identity_binding(authorization, observed, validated)
    assert exc.value.reason == "OLLAMA_ROUTE_OBSERVED"


def test_runtime_identity_rejects_alias_rewriting(manifest, tmp_path, synthetic_executable):
    configuration = _valid_configuration(manifest, tmp_path, synthetic_executable)
    authorization = _valid_authorization(manifest, tmp_path)
    observed = _observed(manifest, model="deepseek-v4-flash-alias")
    validated = adapter.validate_adapter_configuration_structure(configuration)
    validated["authorization_hash"] = runner.authorization_hash(authorization)
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.build_runtime_identity_binding(authorization, observed, validated)
    assert exc.value.reason == "MODEL_FAMILY_MISMATCH"


# ---- template artifact -------------------------------------------------------


def test_template_writer_creates_non_executable_template(tmp_path):
    target = tmp_path / "adapter-template.json"
    written = adapter.write_adapter_configuration_template(target)
    assert written == target
    value = json.loads(target.read_text(encoding="utf-8"))
    assert value["template"] is True
    with pytest.raises(adapter.AdapterConfigurationError) as exc:
        adapter.validate_adapter_configuration_structure(value)
    assert exc.value.reason == "TEMPLATE_IS_NOT_CONFIGURATION"


def test_template_writer_refuses_existing_target(tmp_path):
    target = tmp_path / "adapter-template.json"
    adapter.write_adapter_configuration_template(target)
    with pytest.raises(adapter.OpenCodeGoAdapterError):
        adapter.write_adapter_configuration_template(target)
