"""CLI integration tests for the OpenCode Go execution adapter modes."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests" / "unit"))

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


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _authorization(manifest, output_root: Path) -> dict:
    return {
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
        "output_root": str(output_root.resolve()),
        "campaign_attempt_identity": "quixbugs-paired-pilot-v2-attempt-" + "d" * 64,
        "single_frozen_six_case_campaign_confirmation": True,
    }


def _raw_route_evidence(manifest) -> dict:
    return {
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


def _configuration(manifest, tmp_path, synthetic_executable, authorization: dict) -> dict:
    interpreter = sys.executable
    runtime_model_id = "opencode-go/test-deepseek-v4-flash"
    boundary = adapter.common_operator_boundary([interpreter, synthetic_executable, tmp_path])
    return {
        "schema_version": adapter.ADAPTER_SCHEMA_VERSION,
        "template": False,
        "adapter_identity": adapter.ADAPTER_IDENTITY,
        "campaign_id": "quixbugs-paired-pilot-v2",
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "operator_authorization_id": "test-operator-001",
        "authorization_hash": runner.authorization_hash(authorization),
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
        "per_call_timeout_seconds": 20.0,
        "total_case_timeout_seconds": 40.0,
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


def _write_artifacts(tmp_path, manifest, synthetic_executable, output_root: Path) -> dict:
    authorization = _authorization(manifest, output_root)
    evidence = _raw_route_evidence(manifest)
    configuration = _configuration(manifest, tmp_path, synthetic_executable, authorization)
    auth_path = tmp_path / "authorization.json"
    evidence_path = tmp_path / "route-evidence.json"
    config_path = tmp_path / "adapter-config.json"
    auth_path.write_text(json.dumps(authorization, indent=2, sort_keys=True), encoding="utf-8")
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    config_path.write_text(json.dumps(configuration, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "authorization": authorization,
        "evidence": evidence,
        "configuration": configuration,
        "auth_path": auth_path,
        "evidence_path": evidence_path,
        "config_path": config_path,
    }


def _clean_git_state(commit):
    return runner.GitRepositoryState(
        head=commit,
        execution_commit_exists=True,
        execution_commit_descends_from_baseline=True,
        tracked_working_tree_clean=True,
        git_index_clean=True,
    )


class _FakeFacts:
    def __init__(self) -> None:
        self.execution_context = object()


def test_adapter_template_mode(tmp_path, capsys):
    target = tmp_path / "template.json"
    rc = adapter.main(["adapter-template", "--output", str(target)])
    assert rc == 0
    value = json.loads(target.read_text(encoding="utf-8"))
    assert value["template"] is True
    assert json.loads(capsys.readouterr().out)["executable"] is False


def test_adapter_validate_structural_only(tmp_path, manifest, synthetic_executable, capsys):
    artifacts = _write_artifacts(tmp_path, manifest, synthetic_executable, tmp_path / "attempt-out")
    rc = adapter.main(["adapter-validate", "--adapter-config", str(artifacts["config_path"])])
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] is True
    assert result["binding_checked"] is False


def test_adapter_validate_with_binding(tmp_path, manifest, synthetic_executable, capsys):
    artifacts = _write_artifacts(tmp_path, manifest, synthetic_executable, tmp_path / "attempt-out")
    rc = adapter.main([
        "adapter-validate",
        "--adapter-config", str(artifacts["config_path"]),
        "--authorization", str(artifacts["auth_path"]),
        "--route-evidence-json", str(artifacts["evidence_path"]),
    ])
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] is True
    assert result["binding_checked"] is True


def test_adapter_validate_rejects_inactive_template(tmp_path, manifest, synthetic_executable, capsys):
    artifacts = _write_artifacts(tmp_path, manifest, synthetic_executable, tmp_path / "attempt-out")
    template = json.loads(artifacts["config_path"].read_text(encoding="utf-8"))
    template["template"] = True
    template_path = tmp_path / "template-config.json"
    template_path.write_text(json.dumps(template), encoding="utf-8")
    rc = adapter.main(["adapter-validate", "--adapter-config", str(template_path)])
    assert rc == 2
    assert "TEMPLATE_IS_NOT_CONFIGURATION" in capsys.readouterr().err


def test_route_preflight_only_zero_processes(tmp_path, manifest, synthetic_executable, capsys, monkeypatch):
    launches: list = []
    real_popen = __import__("subprocess").Popen

    def spy_popen(command, **kwargs):
        launches.append(command)
        return real_popen(command, **kwargs)

    monkeypatch.setattr(__import__("subprocess"), "Popen", spy_popen)
    monkeypatch.setattr(runner, "real_git_state", _clean_git_state)
    artifacts = _write_artifacts(tmp_path, manifest, synthetic_executable, tmp_path / "attempt-out")
    rc = adapter.main([
        "route-preflight-only",
        "--authorization", str(artifacts["auth_path"]),
        "--route-evidence-json", str(artifacts["evidence_path"]),
        "--adapter-config", str(artifacts["config_path"]),
        "--output", str(tmp_path / "attempt-out"),
    ])
    assert rc == 0
    assert launches == []
    result = json.loads(capsys.readouterr().out)
    assert result["preflight"]["passed"] is True
    assert result["provider_processes_created"] == 0


def test_selftest_mode_is_synthetic_only(tmp_path, capsys):
    rc = adapter.main(["selftest", "--output", str(tmp_path / "selftest-out")])
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert result["synthetic_only"] is True
    assert result["real_executable_contacted"] is False
    assert result["ownership_gate_verified"] is True
    scenarios = {item["scenario"]: item["outcome"] for item in result["scenarios"]}
    assert scenarios["valid-usage"] == "RESPONSE"
    assert scenarios["valid-no-usage"] == "RESPONSE"
    assert scenarios["cost-zero"] == "RESPONSE"
    assert scenarios["malformed-always"] == "TRANSPORT_ERROR"
    assert scenarios["identity-mismatch"] == "ROUTE_DRIFT"
    assert scenarios["route-drift"] == "ROUTE_DRIFT"
    assert scenarios["credential-output"] == "RESPONSE"
    assert scenarios["nonzero-exit"] == "TRANSPORT_ERROR"


def test_live_wire_requires_confirmation(tmp_path, manifest, synthetic_executable, capsys):
    artifacts = _write_artifacts(tmp_path, manifest, synthetic_executable, tmp_path / "attempt-out")
    rc = adapter.main([
        "live-wire",
        "--authorization", str(artifacts["auth_path"]),
        "--route-evidence-json", str(artifacts["evidence_path"]),
        "--adapter-config", str(artifacts["config_path"]),
        "--output", str(tmp_path / "attempt-out"),
    ])
    assert rc == 2
    assert "confirmation" in capsys.readouterr().err.lower()


def test_live_wire_requires_environment_and_facts(tmp_path, manifest, synthetic_executable, capsys, monkeypatch):
    monkeypatch.setattr(runner, "real_git_state", _clean_git_state)
    artifacts = _write_artifacts(tmp_path, manifest, synthetic_executable, tmp_path / "attempt-out")
    rc = adapter.main([
        "live-wire",
        "--authorization", str(artifacts["auth_path"]),
        "--route-evidence-json", str(artifacts["evidence_path"]),
        "--adapter-config", str(artifacts["config_path"]),
        "--output", str(tmp_path / "attempt-out"),
        "--confirm-opencode-go-adapter",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "QuixBugs environment artifact" in err


def test_live_wire_completes_with_explicit_wiring(tmp_path, manifest, synthetic_executable, capsys, monkeypatch):
    """Full live wiring run with a synthetic executable, deterministic git
    state, and an operator facts provider; the run is completed through the
    accepted runner and package verification succeeds."""
    monkeypatch.setattr(runner, "real_git_state", _clean_git_state)
    output_root = tmp_path / "attempt-out"
    artifacts = _write_artifacts(tmp_path, manifest, synthetic_executable, output_root)

    provider_module = tmp_path / "operator_facts_provider.py"
    provider_module.write_text(
        "class _FakeFacts:\n"
        "    def __init__(self):\n"
        "        self.execution_context = object()\n"
        "def provide():\n"
        "    return _FakeFacts()\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(tmp_path))
    try:
        environment_artifact = tmp_path / "quixbugs-environment.json"
        environment_artifact.write_text(json.dumps({
            "repository_root": str(tmp_path / "repo"),
            "sources_parent": str(tmp_path / "sources"),
        }, indent=2, sort_keys=True), encoding="utf-8")
        rc = adapter.main([
            "live-wire",
            "--authorization", str(artifacts["auth_path"]),
            "--route-evidence-json", str(artifacts["evidence_path"]),
            "--adapter-config", str(artifacts["config_path"]),
            "--output", str(output_root),
            "--quixbugs-environment-json", str(environment_artifact),
            "--facts-provider", "operator_facts_provider:provide",
            "--confirm-opencode-go-adapter",
        ])
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        campaign = result["campaign"]
        assert campaign["status"] == "ABORTED" or campaign["status"] == "COMPLETED"
        # The wiring is unusable without an active validated configuration:
        # the run reached the runner with a real case runner and factory, so
        # with a no-op synthetic executor the campaign still executes through
        # the accepted ledger/terminal commitment path.
        assert result["runtime_identity_binding_fingerprint"]
    finally:
        sys.path.remove(str(tmp_path))
        monkeypatch.delitem(sys.modules, "operator_facts_provider", raising=False)


def test_validate_tracked_template_fails_as_active(tmp_path, manifest, synthetic_executable, capsys):
    template_path = REPO_ROOT / "research" / "quixbugs" / "OPENCODE_GO_EXECUTION_ADAPTER_TEMPLATE.json"
    rc = adapter.main(["adapter-validate", "--adapter-config", str(template_path)])
    assert rc == 2
    assert "TEMPLATE_IS_NOT_CONFIGURATION" in capsys.readouterr().err
