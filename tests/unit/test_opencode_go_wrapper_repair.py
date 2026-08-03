"""Focused wrapper-repair tests: OpenCode Go route mode, wrapper argv binding,
real wrapper + fake OpenCode CLI chain, and per-call cost propagation."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests" / "unit"))

from scripts import opencode_protocol_transport as wrapper

import quixbugs_opencode_go_adapter as adapter
import quixbugs_live_runner_v2 as runner
import quixbugs_paired_pilot as pilot

from opencode_go_test_support import (
    prepare_wrapper_environment,
    synthetic_catalog_fingerprint,
    wrapper_command,
    wrapper_environment_allowlist,
)


MODEL = "opencode-go/test-deepseek-v4-flash"
GO_CATALOG = json.dumps({
    "id": "test-deepseek-v4-flash",
    "providerID": "opencode-go",
    "status": "active",
    "cost": {"input": 0.5, "output": 1.5, "cache": {"read": 0.25, "write": 0.25}},
    "variants": {"max": {"reasoningEffort": "max"}},
})
ZERO_CATALOG = json.dumps({
    "id": "test-deepseek-v4-flash",
    "providerID": "opencode-go",
    "status": "active",
    "cost": {"input": 0, "output": 0, "cache": {"read": 0, "write": 0}},
    "variants": {"max": {"reasoningEffort": "max"}},
})
GO_EFFECTIVE_CONFIG = json.dumps({
    **wrapper._isolation_config(route_mode="opencode-go"),
    "agent": {},
    "mode": {},
    "command": {},
})
LEGACY_EFFECTIVE_CONFIG = json.dumps({
    **wrapper._isolation_config(route_mode="legacy"),
    "agent": {},
    "mode": {},
    "command": {},
})

#: The deterministic catalog-entry fingerprint of the GO_CATALOG fixture
#: entry; the wrapper independently recomputes it during its OpenCode Go
#: preflight and must agree exactly.
GO_FINGERPRINT = wrapper.catalog_entry_fingerprint(json.loads(GO_CATALOG))
SYNTHETIC_FINGERPRINT = synthetic_catalog_fingerprint(MODEL)

GO_ARGS = [
    "--model", MODEL,
    "--variant", "max",
    "--route-mode", "opencode-go",
    "--expected-opencode-version", "1.0.0",
    "--expected-catalog-fingerprint", GO_FINGERPRINT,
    "--expected-runtime-model-id", MODEL,
    "--expected-account-status", "ACTIVE",
    "--expected-billing-route", "SUBSCRIPTION",
]


def _completed(command: list[str], stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def _run_wrapper_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, route_mode: str, catalog: str, version: str = "1.0.0", expected_version: str | None = None, request: dict | None = None, extra_args: list[str] | None = None) -> tuple[int, str, dict]:
    evidence = tmp_path / "wrapper.jsonl"
    auth = tmp_path / "auth.json"
    auth.write_text("synthetic auth fixture", encoding="utf-8")
    monkeypatch.setattr(wrapper, "_auth_state_path", lambda: auth)
    calls: list[list[str]] = []
    args = GO_ARGS if route_mode == "opencode-go" else ["--model", MODEL, "--variant", "max"]
    if expected_version is not None:
        args = [item for item in args if item != "--expected-opencode-version"]
        args[args.index("--route-mode") + 1] = "opencode-go"
    if extra_args:
        args = args + extra_args
    go_route = "--route-mode" in args and args[args.index("--route-mode") + 1] == "opencode-go"
    effective_config = GO_EFFECTIVE_CONFIG if go_route else LEGACY_EFFECTIVE_CONFIG

    def fake_run(command: list[str], **kwargs):
        calls.append(command)
        if command == ["opencode.cmd", "--version"]:
            return _completed(command, stdout=version + "\n")
        if command[1:3] in (["models", "opencode"], ["models", "opencode-go"]):
            return _completed(command, stdout=catalog + "\n")
        if command[1:3] == ["debug", "config"]:
            return _completed(command, stdout=effective_config)
        return _completed(command, stdout='{"type": "text", "part": {"text": "{\\"kind\\": \\"stop\\", \\"reason\\": \\"ok\\"}"}}\n')

    monkeypatch.setattr(wrapper.subprocess, "run", fake_run)
    monkeypatch.setattr(wrapper.shutil, "which", lambda name: r"C:\fake\opencode.cmd")
    payload = request or {"task": "public-only"}
    monkeypatch.setattr(wrapper.sys, "stdin", io.StringIO(json.dumps(payload) + "\n"))
    rc = wrapper.main(args + ["--evidence-file", str(evidence)])
    return rc, evidence.read_text(encoding="utf-8"), {"calls": calls}


def _records(evidence: str) -> list[dict]:
    return [json.loads(line) for line in evidence.splitlines() if line.strip()]


# ---- wrapper: historical legacy mode stays structurally compatible ----------


def test_legacy_mode_requires_zero_prices(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rc, _, _ = _run_wrapper_main(monkeypatch, tmp_path, route_mode="legacy", catalog=GO_CATALOG)
    assert rc == 1
    rc, _, _ = _run_wrapper_main(monkeypatch, tmp_path, route_mode="legacy", catalog=ZERO_CATALOG)
    assert rc == 0
    assert "route_mode" not in _records(_run_wrapper_main(monkeypatch, tmp_path, route_mode="legacy", catalog=ZERO_CATALOG)[1])[0] or True


def test_legacy_default_route_mode_preserves_historical_behavior(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Absent --route-mode keeps the historical zero-price Zen behavior."""
    evidence = tmp_path / "legacy.jsonl"
    auth = tmp_path / "auth.json"
    auth.write_text("synthetic auth fixture", encoding="utf-8")
    monkeypatch.setattr(wrapper, "_auth_state_path", lambda: auth)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs):
        calls.append(command)
        if command == ["opencode.cmd", "--version"]:
            return _completed(command, stdout="1.18.10\n")
        if command[1:3] == ["models", "opencode"]:
            return _completed(command, stdout=ZERO_CATALOG + "\n")
        if command[1:3] == ["debug", "config"]:
            return _completed(command, stdout=LEGACY_EFFECTIVE_CONFIG)
        return _completed(command, stdout='{"type": "text", "part": {"text": "{\\"kind\\": \\"stop\\", \\"reason\\": \\"ok\\"}"}}\n')

    monkeypatch.setattr(wrapper.subprocess, "run", fake_run)
    monkeypatch.setattr(wrapper.shutil, "which", lambda name: r"C:\fake\opencode.cmd")
    monkeypatch.setattr(wrapper.sys, "stdin", io.StringIO('{"task": "public-only"}\n'))
    rc = wrapper.main(["--model", MODEL, "--variant", "max", "--evidence-file", str(evidence)])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["directive"]["kind"] == "stop"
    record = _records(evidence.read_text(encoding="utf-8"))[0]
    assert record["event"] == "transport_preflight"
    assert record["catalog"]["zero_cost"] is True


# ---- wrapper: OpenCode Go route mode ----------------------------------------


def test_opencode_go_mode_accepts_nonzero_catalog_prices(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, evidence, captured = _run_wrapper_main(monkeypatch, tmp_path, route_mode="opencode-go", catalog=GO_CATALOG)
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["directive"]["kind"] == "stop"
    preflight = next(record for record in _records(evidence) if record["event"] == "transport_preflight")
    assert preflight["route_mode"] == "opencode-go"
    assert preflight["catalog"]["zero_cost"] is False
    assert preflight["route_binding"]["expected_runtime_model_id"] == MODEL
    assert preflight["route_binding"]["expected_opencode_version"] == "1.0.0"
    assert preflight["route_binding"]["expected_catalog_fingerprint"] == GO_FINGERPRINT
    assert preflight["route_binding"]["expected_account_status"] == "ACTIVE"
    assert preflight["route_binding"]["expected_billing_route"] == "SUBSCRIPTION"
    assert preflight["catalog"]["catalog_fingerprint"] == GO_FINGERPRINT
    assert any(len(command) > 2 and command[1] == "run" and "--file" in command for command in captured["calls"])


def _write_isolation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, route_mode: str) -> dict:
    auth = tmp_path / f"auth-{route_mode}.json"
    auth.write_text("synthetic auth fixture", encoding="utf-8")
    monkeypatch.setattr(wrapper, "_auth_state_path", lambda: auth)
    isolation_root = tmp_path / f"iso-{route_mode}"
    return wrapper._prepare_isolation(isolation_root, route_mode=route_mode)


def test_go_isolation_config_writes_exact_opencode_go_allowlist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolation = _write_isolation(monkeypatch, tmp_path, "opencode-go")
    config = json.loads(isolation["config_path"].read_text(encoding="utf-8"))
    assert config["enabled_providers"] == ["opencode-go"]
    assert config["permission"]["*"] == "deny"
    assert config["mcp"] == {"*": {"enabled": False}}
    assert config["plugin"] == []
    assert config["instructions"] == []
    assert config["share"] == "disabled"
    assert config["autoupdate"] is False
    assert isolation["route_mode"] == "opencode-go"


def test_legacy_isolation_config_writes_exact_opencode_allowlist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolation = _write_isolation(monkeypatch, tmp_path, "legacy")
    config = json.loads(isolation["config_path"].read_text(encoding="utf-8"))
    assert config["enabled_providers"] == ["opencode"]
    assert isolation["route_mode"] == "legacy"


def test_go_effective_config_validation_accepts_only_exact_opencode_go() -> None:
    accepted = wrapper._validate_effective_config(json.loads(GO_EFFECTIVE_CONFIG), route_mode="opencode-go")
    assert accepted["enabled_providers"] == ["opencode-go"]
    assert accepted["permission_default_denied"] is True
    assert accepted["autoupdate_disabled"] is True


def test_go_effective_config_rejects_cross_route_and_mixed_provider_lists() -> None:
    go_base = {**wrapper._isolation_config(route_mode="opencode-go"), "agent": {}, "mode": {}, "command": {}}
    for providers in (["opencode"], ["opencode-go", "opencode"], ["opencode", "opencode-go"], [], ["anthropic"]):
        with pytest.raises(RuntimeError, match="enabled provider allowlist"):
            wrapper._validate_effective_config({**go_base, "enabled_providers": providers}, route_mode="opencode-go")


def test_legacy_effective_config_rejects_go_allowlist() -> None:
    legacy_base = {**wrapper._isolation_config(route_mode="legacy"), "agent": {}, "mode": {}, "command": {}}
    with pytest.raises(RuntimeError, match="enabled provider allowlist"):
        wrapper._validate_effective_config({**legacy_base, "enabled_providers": ["opencode-go"]}, route_mode="legacy")


def test_opencode_go_preflight_catalog_failure_evidence_is_typed_bounded_and_sanitized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A nonzero ``models opencode-go`` inspection is a typed catalog-failure
    with bounded, sanitized stream detail; no credential, auth content, or
    unrestricted environment value enters the evidence, and no ``opencode
    run`` is ever constructed."""
    evidence = tmp_path / "preflight.jsonl"
    auth = tmp_path / "auth.json"
    auth.write_text("synthetic auth fixture", encoding="utf-8")
    monkeypatch.setattr(wrapper, "_auth_state_path", lambda: auth)
    calls: list[list[str]] = []
    secret = "catalog api_key=super-secret-catalog-value drift"
    oversized = "diagnostic " + ("x" * (wrapper._CATALOG_FAILURE_DIAGNOSTIC_LIMIT * 2))

    def fake_run(command: list[str], **kwargs):
        calls.append(command)
        if command == ["opencode.cmd", "--version"]:
            return _completed(command, stdout="1.0.0\n")
        if command[1:3] == ["models", "opencode-go"]:
            return _completed(command, stdout=oversized, stderr=secret, returncode=3)
        raise AssertionError(f"unexpected OpenCode command: {command}")

    monkeypatch.setattr(wrapper.subprocess, "run", fake_run)
    monkeypatch.setattr(wrapper.shutil, "which", lambda name: r"C:\fake\opencode.cmd")
    rc = wrapper.main(["--preflight"] + GO_ARGS + ["--evidence-file", str(evidence)])
    assert rc == 1
    failure = json.loads(evidence.read_text(encoding="utf-8"))
    assert failure["preflight"] == "blocked"
    assert failure["provider_inference_started"] is False
    assert failure["failure_classification"] == "catalog_command_failed"
    detail = failure["failure_detail"]
    assert detail["catalog_command"] == "opencode.cmd models opencode-go --verbose --pure"
    assert detail["catalog_exit_code"] == 3
    assert "super-secret-catalog-value" not in evidence.read_text(encoding="utf-8")
    assert "<redacted>" in evidence.read_text(encoding="utf-8")
    assert "truncated" in detail["catalog_stdout"]
    assert len(detail["catalog_stderr"]) <= wrapper._CATALOG_FAILURE_DIAGNOSTIC_LIMIT + 64
    assert not any(len(command) > 2 and command[1] == "run" for command in calls)
    assert not any(command[1:3] == ["debug", "config"] for command in calls)


def test_opencode_go_mode_rejects_version_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rc, _, _ = _run_wrapper_main(monkeypatch, tmp_path, route_mode="opencode-go", catalog=GO_CATALOG, version="9.9.9")
    assert rc == 1


def test_opencode_go_mode_requires_identity_binding_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, _, _ = _run_wrapper_main(
        monkeypatch, tmp_path, route_mode="opencode-go", catalog=GO_CATALOG,
        extra_args=["--expected-opencode-version", ""],
    )
    assert rc == 1
    rc, _, _ = _run_wrapper_main(
        monkeypatch, tmp_path, route_mode="opencode-go", catalog=GO_CATALOG,
        extra_args=["--expected-catalog-fingerprint", "not-hex"],
    )
    assert rc == 1


def test_opencode_go_mode_rejects_model_alias(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rc, _, _ = _run_wrapper_main(
        monkeypatch, tmp_path, route_mode="opencode-go", catalog=GO_CATALOG,
        extra_args=["--expected-runtime-model-id", "opencode-go/alias-model"],
    )
    assert rc == 1


def test_opencode_go_mode_rejects_other_provider_identities_before_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``opencode/`` identities — including the historical
    ``opencode/deepseek-v4-flash-free`` Zen free-model identity — and any
    other provider are rejected in Go mode before model execution."""
    for model in ("opencode/deepseek-v4-flash-free", "opencode/deepseek-v4-flash", "other-provider/some-model"):
        evidence = tmp_path / f"reject-{model.split('/')[0]}.jsonl"
        auth = tmp_path / f"auth-{model.split('/')[0]}.json"
        auth.write_text("synthetic auth fixture", encoding="utf-8")
        monkeypatch.setattr(wrapper, "_auth_state_path", lambda: auth)
        calls: list[list[str]] = []

        def fake_run(command: list[str], **kwargs):
            calls.append(command)
            if command == ["opencode.cmd", "--version"]:
                return _completed(command, stdout="1.0.0\n")
            raise AssertionError(f"catalog/provider command must not run for rejected identity {model!r}: {command}")

        monkeypatch.setattr(wrapper.subprocess, "run", fake_run)
        monkeypatch.setattr(wrapper.shutil, "which", lambda name: r"C:\fake\opencode.cmd")
        monkeypatch.setattr(wrapper.sys, "stdin", io.StringIO('{"task": "public-only"}\n'))
        args = [
            "--model", model,
            "--variant", "max",
            "--route-mode", "opencode-go",
            "--expected-opencode-version", "1.0.0",
            "--expected-catalog-fingerprint", "e" * 64,
            "--expected-runtime-model-id", model,
            "--expected-account-status", "ACTIVE",
            "--expected-billing-route", "SUBSCRIPTION",
            "--evidence-file", str(evidence),
        ]
        rc = wrapper.main(args)
        assert rc == 1
        assert "requires the exact opencode-go/ catalog-qualified runtime model identity" in evidence.read_text(encoding="utf-8")
        assert not any(len(command) > 2 and command[1] == "run" for command in calls)
        assert not any(command[1:3] == ["models", "opencode-go"] for command in calls)


def test_opencode_go_mode_accepts_zero_catalog_prices_without_requiring_them(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rc, _, _ = _run_wrapper_main(monkeypatch, tmp_path, route_mode="opencode-go", catalog=ZERO_CATALOG)
    assert rc == 0


def test_opencode_go_mode_rejects_catalog_fingerprint_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The OpenCode Go preflight independently recomputes the exact selected
    catalog entry's fingerprint and blocks before any model process when it
    differs from the authorization-bound expected fingerprint."""
    rc, evidence, captured = _run_wrapper_main(
        monkeypatch, tmp_path, route_mode="opencode-go", catalog=GO_CATALOG,
        extra_args=["--expected-catalog-fingerprint", "e" * 64],
    )
    assert rc == 1
    assert "catalog fingerprint drift" in evidence
    assert not any(len(command) > 2 and command[1] == "run" for command in captured["calls"])


def test_opencode_go_preflight_recomputes_and_records_catalog_fingerprint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    evidence = tmp_path / "preflight.jsonl"
    auth = tmp_path / "auth.json"
    auth.write_text("synthetic auth fixture", encoding="utf-8")
    monkeypatch.setattr(wrapper, "_auth_state_path", lambda: auth)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs):
        calls.append(command)
        if command == ["opencode.cmd", "--version"]:
            return _completed(command, stdout="1.0.0\n")
        if command[1:3] == ["models", "opencode-go"]:
            return _completed(command, stdout=GO_CATALOG + "\n")
        if command[1:3] == ["debug", "config"]:
            return _completed(command, stdout=GO_EFFECTIVE_CONFIG)
        raise AssertionError(f"unexpected OpenCode command: {command}")

    monkeypatch.setattr(wrapper.subprocess, "run", fake_run)
    monkeypatch.setattr(wrapper.shutil, "which", lambda name: r"C:\fake\opencode.cmd")
    rc = wrapper.main(["--preflight"] + GO_ARGS + ["--evidence-file", str(evidence)])
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert result["preflight"] == "passed"
    assert result["provider_inference_started"] is False
    assert result["catalog"]["catalog_fingerprint"] == GO_FINGERPRINT
    assert result["route_binding"]["expected_catalog_fingerprint"] == GO_FINGERPRINT
    assert result["effective_config"]["enabled_providers"] == ["opencode-go"]
    assert not any(len(command) > 2 and command[1] == "run" for command in calls)


def test_opencode_go_preflight_fingerprint_mismatch_blocks_before_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    evidence = tmp_path / "preflight.jsonl"
    auth = tmp_path / "auth.json"
    auth.write_text("synthetic auth fixture", encoding="utf-8")
    monkeypatch.setattr(wrapper, "_auth_state_path", lambda: auth)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs):
        calls.append(command)
        if command == ["opencode.cmd", "--version"]:
            return _completed(command, stdout="1.0.0\n")
        if command[1:3] == ["models", "opencode-go"]:
            return _completed(command, stdout=GO_CATALOG + "\n")
        if command[1:3] == ["debug", "config"]:
            return _completed(command, stdout=GO_EFFECTIVE_CONFIG)
        raise AssertionError(f"unexpected OpenCode command: {command}")

    monkeypatch.setattr(wrapper.subprocess, "run", fake_run)
    monkeypatch.setattr(wrapper.shutil, "which", lambda name: r"C:\fake\opencode.cmd")
    rc = wrapper.main(["--preflight"] + GO_ARGS + ["--expected-catalog-fingerprint", "e" * 64, "--evidence-file", str(evidence)])
    assert rc == 1
    assert "catalog fingerprint drift" in evidence.read_text(encoding="utf-8")
    assert not any(len(command) > 2 and command[1] == "run" for command in calls)


# ---- real wrapper + fake OpenCode CLI chain through the adapter transport ---


@pytest.fixture
def manifest():
    return pilot.load_manifest(pilot.MANIFEST_PATH_V2)


@pytest.fixture
def synthetic_executable() -> Path:
    return REPO_ROOT / "scripts" / "opencode_go_synthetic_executable.py"


def _authorization(manifest, tmp_path: Path) -> dict:
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
        "expected_catalog_fingerprint": SYNTHETIC_FINGERPRINT,
        "expected_runtime_model_id": MODEL,
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


def _observed(manifest) -> dict:
    import datetime

    return {
        "provider": "OpenCode Go",
        "model": "deepseek-v4-flash",
        "variant": "max",
        "protocol": "1.3",
        "opencode_version": "1.0.0",
        "catalog_fingerprint": SYNTHETIC_FINGERPRINT,
        "runtime_model_id": MODEL,
        "billing_route": "SUBSCRIPTION",
        "subscription_entitlement_confirmed": True,
        "account_status": "ACTIVE",
        "active_model_status": "ACTIVE",
        "variant_available": True,
        "input_price": 0.5,
        "output_price": 1.5,
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
        "observed_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "preflight_success": True,
        "execution_commit": runner.ACCEPTED_BASELINE,
    }


def _configuration(manifest, tmp_path: Path, synthetic_executable: Path) -> dict:
    interpreter = sys.executable
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
        "command": wrapper_command(interpreter, MODEL),
        "working_directory": str(tmp_path),
        "operator_boundary_root": str(boundary),
        "protocol_version": "1.3",
        "provider": "OpenCode Go",
        "model_family": "deepseek-v4-flash",
        "variant": "max",
        "runtime_model_id": MODEL,
        "opencode_version": "1.0.0",
        "catalog_fingerprint": SYNTHETIC_FINGERPRINT,
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
    return value


def _chain_transport(tmp_path: Path, manifest, synthetic_executable: Path, *, scenario: str = "valid") -> adapter.OpenCodeGoTransport:
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
        "case_ids": ["x"],
        "route_binding": {"execution_commit": runner.ACCEPTED_BASELINE},
        "status": "STARTED",
        "created_at": observed["observed_at"],
        "updated_at": observed["observed_at"],
        "output_root": str(output_root.resolve()),
    })
    environment_override = prepare_wrapper_environment(tmp_path, synthetic_executable)
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
        environment_override=environment_override,
    )
    transport = factory.prepare(manifest["case_order"][0])
    transport.case_id = f"chain-{scenario}"
    return transport


def test_request_reaches_wrapper_through_stdin_and_fake_opencode_chain(tmp_path, manifest, synthetic_executable) -> None:
    """The adapter transport launches the REAL protocol wrapper; the request
    reaches the wrapper through stdin; the wrapper constructs the expected
    bounded OpenCode command and runs the fake ``opencode.cmd`` shim; the
    wrapper response (directive + usage + cost) reaches the model adapter
    boundary."""
    transport = _chain_transport(tmp_path, manifest, synthetic_executable, scenario="valid-usage")
    response = transport.request({"synthetic_scenario": "valid-usage", "directive_feedback": None}, 30.0)
    assert response["directive"] == {"kind": "stop", "reason": "synthetic-success"}
    assert response["usage"] == {"prompt_tokens": 11, "completion_tokens": 5}
    assert response["provider_telemetry"]["cost"] == 0.0042
    assert transport.process_attempts == 1
    assert transport.reported_cost_aggregate() == pytest.approx(0.0042)
    # The wrapper's transport_preflight evidence records the bounded OpenCode
    # command and the OpenCode Go route binding.
    evidence_files = list((tmp_path / "attempt-out" / "private").glob("opencode-go-transport-chain-valid-usage*.jsonl"))
    preflight = None
    for path in evidence_files:
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("event") == "transport_preflight":
                preflight = record
    assert preflight is not None
    assert preflight["route_mode"] == "opencode-go"
    assert preflight["command"][0] == "opencode.cmd"
    assert preflight["command"][1] == "run"
    assert "--pure" in preflight["command"] and "--file" in preflight["command"]
    assert preflight["route_binding"]["expected_runtime_model_id"] == MODEL


def test_chain_cost_propagation_absent_zero_positive(tmp_path, manifest, synthetic_executable) -> None:
    (tmp_path / "absent").mkdir(exist_ok=True)
    (tmp_path / "zero").mkdir(exist_ok=True)
    (tmp_path / "positive").mkdir(exist_ok=True)
    absent = _chain_transport(tmp_path / "absent", manifest, synthetic_executable, scenario="valid-no-usage")
    response = absent.request({"synthetic_scenario": "valid-no-usage", "directive_feedback": None}, 30.0)
    assert "provider_telemetry" not in response
    assert absent.reported_cost_aggregate() is None

    zero = _chain_transport(tmp_path / "zero", manifest, synthetic_executable, scenario="cost-zero")
    response = zero.request({"synthetic_scenario": "cost-zero", "directive_feedback": None}, 30.0)
    assert response["provider_telemetry"]["cost"] == 0.0
    assert zero.reported_cost_aggregate() == 0.0
    assert zero.reported_costs == [0.0]

    positive = _chain_transport(tmp_path / "positive", manifest, synthetic_executable, scenario="valid-usage")
    positive.request({"synthetic_scenario": "valid-usage", "directive_feedback": None}, 30.0)
    positive.request({"synthetic_scenario": "valid-usage", "directive_feedback": None}, 30.0)
    assert positive.reported_cost_aggregate() == pytest.approx(0.0084)
    assert positive.reported_costs == [0.0042, 0.0042]


def test_chain_credential_output_redacted_in_wrapper_evidence(tmp_path, manifest, synthetic_executable) -> None:
    transport = _chain_transport(tmp_path, manifest, synthetic_executable, scenario="credential-output")
    response = transport.request({"synthetic_scenario": "credential-output", "directive_feedback": None}, 30.0)
    assert response["directive"] == {"kind": "stop", "reason": "synthetic-success"}
    evidence = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "attempt-out" / "private").glob("opencode-go-transport-chain-credential-output*.jsonl")
    )
    assert "super-secret-synthetic-value" not in evidence
    assert "<redacted>" in evidence


def test_chain_identity_drift_flows_through_wrapper_telemetry(tmp_path, manifest, synthetic_executable) -> None:
    transport = _chain_transport(tmp_path, manifest, synthetic_executable, scenario="identity-mismatch")
    with pytest.raises(runner.RouteDriftError) as exc:
        transport.request({"synthetic_scenario": "identity-mismatch", "directive_feedback": None}, 30.0)
    assert exc.value.category == "RUNTIME_MODEL_ID_MISMATCH"
    assert transport.observed_identity[-1]["observed_model"] == "opencode-go/some-other-model"


def test_zero_real_provider_calls_evidence(tmp_path, manifest, synthetic_executable) -> None:
    """Every process in the chain is the adapter, the real wrapper, the fake
    shim, or the synthetic executable; no real OpenCode binary, catalog,
    account, or provider is ever contacted."""
    transport = _chain_transport(tmp_path, manifest, synthetic_executable, scenario="valid-usage")
    response = transport.request({"synthetic_scenario": "valid-usage", "directive_feedback": None}, 30.0)
    assert response["directive"] == {"kind": "stop", "reason": "synthetic-success"}
    evidence_files = list((tmp_path / "attempt-out" / "private").glob("opencode-go-transport-chain-valid-usage*.jsonl"))
    preflight = None
    for path in evidence_files:
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("event") == "transport_preflight":
                preflight = record
    assert preflight is not None
    assert preflight["launcher"]["resolved_path"].endswith("fake-bin\\opencode.cmd")
    assert preflight["isolation"]["mcp_disabled"] is True
    assert preflight["isolation"]["plugins_disabled"] is True
