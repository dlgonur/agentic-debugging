"""Transport factory and synthetic-executable scenario tests for the OpenCode Go execution adapter."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

import quixbugs_opencode_go_adapter as adapter
import quixbugs_live_runner_v2 as runner
import quixbugs_paired_pilot as pilot

from opencode_go_test_support import (
    prepare_wrapper_environment,
    synthetic_catalog_fingerprint,
    wrapper_command,
    wrapper_environment_allowlist,
)

RUNTIME_MODEL_ID = "opencode-go/test-deepseek-v4-flash"
FINGERPRINT = synthetic_catalog_fingerprint(RUNTIME_MODEL_ID)


@pytest.fixture
def manifest():
    return pilot.load_manifest(pilot.MANIFEST_PATH_V2)


@pytest.fixture
def synthetic_executable() -> Path:
    return REPO_ROOT / "scripts" / "opencode_go_synthetic_executable.py"


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _authorization(manifest, tmp_path, **overrides) -> dict:
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
        "expected_catalog_fingerprint": FINGERPRINT,
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


def _observed(manifest, **overrides) -> dict:
    value = {
        "provider": "OpenCode Go",
        "model": "deepseek-v4-flash",
        "variant": "max",
        "protocol": "1.3",
        "opencode_version": "1.0.0",
        "catalog_fingerprint": FINGERPRINT,
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
        "preflight_success": True,
        "execution_commit": runner.ACCEPTED_BASELINE,
    }
    value.update(overrides)
    return value


def _configuration(manifest, tmp_path, synthetic_executable, **overrides) -> dict:
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
        "catalog_fingerprint": FINGERPRINT,
        "route_class": "SUBSCRIPTION",
        "expected_account_status": "ACTIVE",
        "per_call_timeout_seconds": 20.0,
        "total_case_timeout_seconds": 40.0,
        "environment_allowlist": wrapper_environment_allowlist(),
        "max_stdout_bytes": 262144,
        "max_stderr_bytes": 262144,
        "max_diagnostic_bytes": 16384,
        "transport_retry_limit": 1,
        "max_transport_attempts_per_logical_call": 2,
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


@pytest.fixture
def harness(tmp_path, manifest, synthetic_executable):
    """A fully claimed and bound transport harness with one per-case transport."""
    authorization = _authorization(manifest, tmp_path)
    configuration = _configuration(manifest, tmp_path, synthetic_executable)
    configuration["authorization_hash"] = runner.authorization_hash(authorization)
    observed = _observed(manifest)
    validated = adapter.validate_adapter_configuration_structure(configuration)
    adapter.bind_adapter_configuration(validated, manifest, authorization, observed)
    binding = adapter.build_runtime_identity_binding(authorization, observed, validated)
    output_root = tmp_path / "attempt-out"
    runner.claim_output_root(
        output_root,
        attempt_identity=authorization["campaign_attempt_identity"],
        authorization_hash=runner.authorization_hash(authorization),
        campaign_manifest_hash=pilot.manifest_hash(manifest),
    )
    ledger = runner.AttemptLedger(output_root / "ledger.json")
    ledger.claim({
        "attempt_identity": authorization["campaign_attempt_identity"],
        "authorization_hash": runner.authorization_hash(authorization),
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "accepted_baseline": runner.ACCEPTED_BASELINE,
        "planning_baseline_commit": manifest["planning_baseline_commit"],
        "execution_commit": runner.ACCEPTED_BASELINE,
        "case_ids": [case["case_id"] for case in manifest["case_order"]],
        "route_binding": {
            "provider": "OpenCode Go", "model": "deepseek-v4-flash",
            "variant": "max", "protocol": "1.3",
            "opencode_version": "1.0.0", "catalog_fingerprint": FINGERPRINT,
            "runtime_model_id": "opencode-go/test-deepseek-v4-flash",
            "billing_route": "SUBSCRIPTION", "execution_commit": runner.ACCEPTED_BASELINE,
        },
        "status": "STARTED",
        "created_at": observed["observed_at"],
        "updated_at": observed["observed_at"],
        "output_root": str(output_root.resolve()),
    })
    factory = adapter.OpenCodeGoTransportFactory(
        authorization=authorization,
        execution_commit=runner.ACCEPTED_BASELINE,
        route_observation=observed,
        configuration=validated,
        binding=binding,
        attempt_identity=authorization["campaign_attempt_identity"],
        output_root=output_root,
        ledger_path=output_root / "ledger.json",
        evidence_dir=output_root / "private",
        environment_override=prepare_wrapper_environment(tmp_path, synthetic_executable),
    )
    return {
        "authorization": authorization,
        "configuration": validated,
        "observed": observed,
        "binding": binding,
        "factory": factory,
        "output_root": output_root,
        "manifest": manifest,
        "environment_override": prepare_wrapper_environment(tmp_path, synthetic_executable),
    }


def _scenario_payload(scenario: str, *, feedback=None) -> dict:
    return {"synthetic_scenario": scenario, "directive_feedback": feedback}


def _scenario_transport(harness, scenario: str, **overrides):
    configuration = dict(harness["configuration"])
    return adapter.OpenCodeGoTransport(
        factory=harness["factory"],
        case_id=f"test-{scenario}",
        command=list(configuration["command"]),
        working_directory=Path(configuration["working_directory"]),
        environment_allowlist=list(configuration["environment_allowlist"]),
        max_stdout_bytes=int(overrides.get("max_stdout_bytes", configuration["max_stdout_bytes"])),
        max_stderr_bytes=int(overrides.get("max_stderr_bytes", configuration["max_stderr_bytes"])),
        max_diagnostic_bytes=int(overrides.get("max_diagnostic_bytes", configuration["max_diagnostic_bytes"])),
        per_call_timeout_seconds=float(overrides.get("per_call_timeout_seconds", configuration["per_call_timeout_seconds"])),
        environment_override=harness["environment_override"],
    )


# ---- no process before gates ------------------------------------------------


def test_factory_requires_validated_inputs(manifest, tmp_path, synthetic_executable):
    authorization = _authorization(manifest, tmp_path)
    configuration = _configuration(manifest, tmp_path, synthetic_executable)
    configuration["authorization_hash"] = runner.authorization_hash(authorization)
    observed = _observed(manifest)
    validated = adapter.validate_adapter_configuration_structure(configuration)
    adapter.bind_adapter_configuration(validated, manifest, authorization, observed)
    binding = adapter.build_runtime_identity_binding(authorization, observed, validated)
    with pytest.raises(adapter.OpenCodeGoAdapterError):
        adapter.OpenCodeGoTransportFactory(
            authorization=authorization,
            execution_commit=runner.ACCEPTED_BASELINE,
            route_observation={"preflight_success": False},
            configuration=validated,
            binding=binding,
            attempt_identity="quixbugs-paired-pilot-v2-attempt-" + "d" * 64,
            output_root=tmp_path / "attempt-out",
        )
    with pytest.raises(adapter.OpenCodeGoAdapterError):
        adapter.OpenCodeGoTransportFactory(
            authorization=authorization,
            execution_commit="b" * 40,
            route_observation=observed,
            configuration=validated,
            binding=binding,
            attempt_identity="quixbugs-paired-pilot-v2-attempt-" + "d" * 64,
            output_root=tmp_path / "attempt-out",
        )
    with pytest.raises(adapter.OpenCodeGoAdapterError):
        adapter.OpenCodeGoTransportFactory(
            authorization=authorization,
            execution_commit=runner.ACCEPTED_BASELINE,
            route_observation=observed,
            configuration=validated,
            binding=binding,
            attempt_identity="not-an-attempt-identity",
            output_root=tmp_path / "attempt-out",
        )


def test_zero_process_before_ownership_gates(tmp_path, manifest, synthetic_executable, monkeypatch):
    """prepare() before the output/attempt ownership gates pass must never
    create a process."""
    launches: list[list[str]] = []
    real_popen = subprocess.Popen

    def spy_popen(command, **kwargs):
        launches.append(list(command))
        return real_popen(command, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy_popen)
    authorization = _authorization(manifest, tmp_path)
    configuration = _configuration(manifest, tmp_path, synthetic_executable)
    configuration["authorization_hash"] = runner.authorization_hash(authorization)
    observed = _observed(manifest)
    validated = adapter.validate_adapter_configuration_structure(configuration)
    adapter.bind_adapter_configuration(validated, manifest, authorization, observed)
    binding = adapter.build_runtime_identity_binding(authorization, observed, validated)
    output_root = tmp_path / "attempt-out"
    factory = adapter.OpenCodeGoTransportFactory(
        authorization=authorization,
        execution_commit=runner.ACCEPTED_BASELINE,
        route_observation=observed,
        configuration=validated,
        binding=binding,
        attempt_identity=authorization["campaign_attempt_identity"],
        output_root=output_root,
    )
    case = manifest["case_order"][0]
    with pytest.raises(adapter.OpenCodeGoAdapterError):
        factory.prepare(case)
    assert launches == []
    assert factory.spawned_processes == 0
    runner.claim_output_root(
        output_root,
        attempt_identity=authorization["campaign_attempt_identity"],
        authorization_hash=runner.authorization_hash(authorization),
        campaign_manifest_hash=pilot.manifest_hash(manifest),
    )
    with pytest.raises(adapter.OpenCodeGoAdapterError):
        factory.prepare(case)
    assert launches == []
    ledger = runner.AttemptLedger(output_root / "ledger.json")
    ledger.claim({
        "attempt_identity": authorization["campaign_attempt_identity"],
        "authorization_hash": runner.authorization_hash(authorization),
        "campaign_manifest_hash": pilot.manifest_hash(manifest),
        "accepted_baseline": runner.ACCEPTED_BASELINE,
        "planning_baseline_commit": manifest["planning_baseline_commit"],
        "execution_commit": runner.ACCEPTED_BASELINE,
        "case_ids": [case["case_id"] for case in manifest["case_order"]],
        "route_binding": {"execution_commit": runner.ACCEPTED_BASELINE},
        "status": "STARTED",
        "created_at": observed["observed_at"],
        "updated_at": observed["observed_at"],
        "output_root": str(output_root.resolve()),
    })
    transport = factory.prepare(case)
    assert transport is not None
    assert launches == []


def test_fresh_transport_per_case(harness, manifest):
    first = harness["factory"].prepare(manifest["case_order"][0])
    second = harness["factory"].prepare(manifest["case_order"][1])
    assert first is not second
    assert first.process_attempts == 0
    assert second.process_attempts == 0
    assert harness["factory"].active_transport is second


def test_transport_spawns_structured_argv_with_bounded_environment(harness, monkeypatch):
    launched: dict = {}

    def spy_popen(command, **kwargs):
        launched["command"] = list(command)
        launched["cwd"] = kwargs.get("cwd")
        launched["env"] = dict(kwargs.get("env") or {})
        launched["shell"] = kwargs.get("shell")
        launched["creationflags"] = kwargs.get("creationflags", 0)

        class FakeProcess:
            def __init__(self):
                self.stdout = None
                self.stderr = None
                self.stdin = None
                self.returncode = 0
                self.pid = 4242

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

            def poll(self):
                return 0

        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", spy_popen)
    transport = harness["factory"].prepare(harness["manifest"]["case_order"][0])
    request_payload = {"protocol": {"name": "test"}, "directive_feedback": None}
    # The fake process returns empty stdout, so the request raises; the
    # assertion target is the spawn contract itself.
    from agentic_debugger.evaluation.live import LiveTransportError

    with pytest.raises(LiveTransportError):
        transport.request(request_payload, 10.0)
    assert launched["shell"] is False
    assert isinstance(launched["command"], list) and launched["command"]
    assert launched["cwd"] == harness["configuration"]["working_directory"]
    assert set(launched["env"]) == {"PATH", "SystemRoot", "USERPROFILE", "HOME", "TMP", "TEMP"}
    assert launched["creationflags"] & getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


# ---- synthetic scenarios -----------------------------------------------------


def test_valid_scenario_success(harness):
    transport = _scenario_transport(harness, "valid")
    response = transport.request(_scenario_payload("valid"), 30.0)
    assert response["directive"] == {"kind": "stop", "reason": "synthetic-success"}
    assert response["usage"]["prompt_tokens"] == 11
    assert response["provider_telemetry"]["cost"] == 0.0042
    assert transport.process_attempts == 1
    assert transport.last_process_exit_code == 0
    assert transport.observed_usage


def test_valid_no_usage_stays_absent(harness):
    transport = _scenario_transport(harness, "valid-no-usage")
    response = transport.request(_scenario_payload("valid-no-usage"), 30.0)
    assert "usage" not in response
    assert "provider_telemetry" not in response
    assert transport.observed_usage == []


def test_malformed_then_valid_recovery(harness):
    transport = _scenario_transport(harness, "malformed-then-valid")
    from agentic_debugger.evaluation.live import LiveTransportError

    with pytest.raises(LiveTransportError) as exc:
        transport.request(_scenario_payload("malformed-then-valid"), 30.0)
    assert exc.value.kind == "process_error"
    assert transport.process_attempts == 1
    response = transport.request(_scenario_payload("malformed-then-valid", feedback={"category": "malformed_directive", "message": "retry"}), 30.0)
    assert response["directive"]["kind"] == "stop"
    assert transport.process_attempts == 2


def test_malformed_exhaustion_no_automatic_retry(harness):
    """The transport itself never retries; retry accounting belongs to the
    accepted live harness (LiveModelAdapter)."""
    transport = _scenario_transport(harness, "malformed-always")
    from agentic_debugger.evaluation.live import LiveTransportError

    with pytest.raises(LiveTransportError):
        transport.request(_scenario_payload("malformed-always"), 30.0)
    assert transport.process_attempts == 1
    with pytest.raises(LiveTransportError):
        transport.request(_scenario_payload("malformed-always", feedback={"category": "x"}), 30.0)
    assert transport.process_attempts == 2


def test_startup_failure_is_process_error(harness):
    transport = _scenario_transport(harness, "startup-failure")
    from agentic_debugger.evaluation.live import LiveTransportError

    with pytest.raises(LiveTransportError) as exc:
        transport.request(_scenario_payload("startup-failure"), 30.0)
    assert exc.value.kind == "process_error"
    assert transport.last_provider_error_category == "process_error"


def test_nonzero_exit_is_process_error(harness):
    transport = _scenario_transport(harness, "nonzero-exit")
    from agentic_debugger.evaluation.live import LiveTransportError

    with pytest.raises(LiveTransportError) as exc:
        transport.request(_scenario_payload("nonzero-exit"), 30.0)
    assert exc.value.kind == "process_error"
    assert transport.last_process_exit_code == 1


def test_timeout_is_bounded_and_typed(harness):
    transport = _scenario_transport(harness, "timeout", per_call_timeout_seconds=2.0)
    from agentic_debugger.evaluation.live import LiveTransportError

    started = time.monotonic()
    with pytest.raises(LiveTransportError) as exc:
        transport.request(_scenario_payload("timeout"), 60.0)
    elapsed = time.monotonic() - started
    assert exc.value.timed_out is True
    assert exc.value.kind == "request_timeout"
    assert elapsed < 15.0
    assert transport.last_timed_out is True


def test_oversized_stdout_is_rejected(harness):
    transport = _scenario_transport(harness, "oversized", max_stdout_bytes=65536, max_stderr_bytes=65536)
    from agentic_debugger.evaluation.live import LiveTransportError

    with pytest.raises(LiveTransportError) as exc:
        transport.request(_scenario_payload("oversized"), 30.0)
    assert exc.value.kind == "process_error"
    assert transport.last_provider_error_category == "process_error"


def test_identity_mismatch_is_route_drift(harness):
    transport = _scenario_transport(harness, "identity-mismatch")
    with pytest.raises(runner.RouteDriftError) as exc:
        transport.request(_scenario_payload("identity-mismatch"), 30.0)
    assert exc.value.category == "RUNTIME_MODEL_ID_MISMATCH"
    assert transport.drift_category == "RUNTIME_MODEL_ID_MISMATCH"
    assert transport.observed_identity[-1]["observed_model"] == "opencode-go/some-other-model"


def test_zen_route_drift_is_rejected(harness):
    transport = _scenario_transport(harness, "route-drift")
    with pytest.raises(runner.RouteDriftError) as exc:
        transport.request(_scenario_payload("route-drift"), 30.0)
    assert exc.value.category == "ZEN_ROUTE_OBSERVED"


def test_free_tier_drift_is_rejected(harness):
    transport = _scenario_transport(harness, "free-tier-drift")
    with pytest.raises(runner.RouteDriftError) as exc:
        transport.request(_scenario_payload("free-tier-drift"), 30.0)
    assert exc.value.category == "FREE_TIER_SUBSTITUTION"


def test_model_substitution_drift_is_rejected(harness):
    transport = _scenario_transport(harness, "model-substitution-drift")
    with pytest.raises(runner.RouteDriftError) as exc:
        transport.request(_scenario_payload("model-substitution-drift"), 30.0)
    assert exc.value.category == "MODEL_SUBSTITUTION_OBSERVED"


def test_nonfinite_usage_metadata_rejected(harness):
    transport = _scenario_transport(harness, "nonfinite-usage")
    from agentic_debugger.evaluation.live import LiveTransportError

    with pytest.raises(LiveTransportError) as exc:
        transport.request(_scenario_payload("nonfinite-usage"), 30.0)
    assert exc.value.kind == "invalid_response"
    assert transport.last_provider_error_category == "non_finite_metadata"


def test_credential_output_is_redacted_from_evidence(harness):
    transport = _scenario_transport(harness, "credential-output")
    response = transport.request(_scenario_payload("credential-output"), 30.0)
    assert response["directive"] == {"kind": "stop", "reason": "synthetic-success"}
    evidence_files = list(harness["output_root"].glob("private/opencode-go-transport-*-*.jsonl"))
    evidence = "\n".join(path.read_text(encoding="utf-8") for path in evidence_files)
    assert "super-secret-synthetic-value" not in evidence
    assert "<redacted>" in evidence


def test_usage_and_cost_metadata_propagated_truthfully(harness):
    transport = _scenario_transport(harness, "valid-usage")
    response = transport.request(_scenario_payload("valid-usage"), 30.0)
    # The wrapper propagates only provider-reported usage fields truthfully;
    # an absent total token count stays absent.
    assert response["usage"] == {"prompt_tokens": 11, "completion_tokens": 5}
    assert "total_tokens" not in response["usage"]
    assert response["provider_telemetry"]["cost"] == 0.0042
    assert transport.observed_usage[-1]["prompt_tokens"] == 11


def test_fresh_process_per_request(harness, monkeypatch):
    pids: list[int] = []
    real_popen = subprocess.Popen

    def spy_popen(command, **kwargs):
        process = real_popen(command, **kwargs)
        pids.append(process.pid)
        return process

    monkeypatch.setattr(subprocess, "Popen", spy_popen)
    transport = _scenario_transport(harness, "valid")
    transport.request(_scenario_payload("valid"), 30.0)
    transport.request(_scenario_payload("valid"), 30.0)
    assert transport.process_attempts == 2
    assert len(pids) == 2
    assert pids[0] != pids[1]


def test_child_process_cleanup_on_timeout(harness):
    """The process-group-aware cleanup must terminate the child tree spawned
    by the fake OpenCode CLI through the real protocol wrapper."""
    transport = _scenario_transport(harness, "timeout-with-child", per_call_timeout_seconds=3.0)
    from agentic_debugger.evaluation.live import LiveTransportError

    with pytest.raises(LiveTransportError) as exc:
        transport.request(_scenario_payload("timeout-with-child"), 60.0)
    assert exc.value.timed_out is True
    markers: list[tuple[float, Path]] = []
    workdir = Path(harness["configuration"]["working_directory"])
    for isolation in workdir.glob("agentic-opencode-transport-*"):
        for marker in isolation.glob("opencode-go-synthetic-child-*.json"):
            try:
                markers.append((marker.stat().st_mtime, marker))
            except OSError:
                continue
    markers.sort(reverse=True)
    child_pid = None
    for _, marker in markers:
        try:
            child_pid = json.loads(marker.read_text(encoding="utf-8"))["child_pid"]
            break
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    assert child_pid is not None
    time.sleep(1.5)
    assert _pid_absent(child_pid)


def _pid_absent(pid: int) -> bool:
    check = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, timeout=15,
    )
    return "No tasks" in (check.stdout or "") or str(pid) not in (check.stdout or "")


def test_binding_revalidated_before_every_attempt(harness):
    transport = harness["factory"].prepare(harness["manifest"]["case_order"][0])
    transport.request(_scenario_payload("valid"), 30.0)
    harness["factory"].configuration["runtime_model_id"] = "opencode-go/tampered-model"
    with pytest.raises(runner.RouteDriftError) as exc:
        transport.request({"directive_feedback": None}, 30.0)
    assert exc.value.category == "RUNTIME_MODEL_ID_MISMATCH"


def test_ownership_gates_revalidated_before_every_attempt(harness):
    transport = harness["factory"].prepare(harness["manifest"]["case_order"][0])
    transport.request(_scenario_payload("valid"), 30.0)
    (harness["output_root"] / "ledger.json").unlink()
    with pytest.raises(adapter.OpenCodeGoAdapterError):
        transport.request({"directive_feedback": None}, 30.0)


def test_evidence_records_independent_observations(harness):
    transport = _scenario_transport(harness, "valid-usage")
    transport.request(_scenario_payload("valid-usage"), 30.0)
    records = []
    for path in harness["output_root"].glob("private/opencode-go-transport-*.jsonl"):
        records.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    response_record = next(item for item in records if item.get("event") == "provider_response")
    assert response_record["provider_exit_code"] == 0
    assert response_record["token_usage"] == {"prompt_tokens": 11, "completion_tokens": 5}
    assert response_record["reported_cost"] == 0.0042
    assert response_record["cost_observed"] is True
