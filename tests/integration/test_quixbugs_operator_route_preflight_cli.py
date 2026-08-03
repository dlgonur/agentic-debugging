"""CLI integration tests for the operator route preflight v1 flow: route
capture through a fake OpenCode shim, operator bundle materialization bound to
the observed clean Git HEAD, adapter validation, and the existing
zero-provider-process route-preflight-only handoff."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests" / "unit"))

import quixbugs_opencode_go_adapter as adapter
import quixbugs_live_runner_v2 as runner
import quixbugs_paired_pilot as pilot
from scripts import opencode_protocol_transport as transport


MODEL = "opencode-go/deepseek-v4-flash"
ATTEMPT = "quixbugs-paired-pilot-v2-attempt-" + "b" * 64
AUTH_ID = "operator-auth-20260803-001"
#: The independently observed clean descendant HEAD the bundle binds; it is
#: deliberately different from the task baseline.
OBSERVED_HEAD = "a" * 40
FAKE_CATALOG_ENTRY = {
    "id": "deepseek-v4-flash",
    "providerID": "opencode-go",
    "status": "active",
    "cost": {"input": 0.5, "output": 1.5, "cache": {"read": 0.25, "write": 0.25}},
    "variants": {"max": {"reasoningEffort": "max"}},
}


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _later_iso(hours: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _completed(command: list[str], stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, "")


def _clean_git_state(commit):
    return runner.GitRepositoryState(
        head=commit,
        execution_commit_exists=True,
        execution_commit_descends_from_baseline=True,
        tracked_working_tree_clean=True,
        git_index_clean=True,
    )


class FakeGit:
    """Deterministic read-only Git command runner standing in for the
    adapter's ``_git`` during the CLI tests; resolves a clean descendant HEAD
    different from the task baseline, optionally drifting on the second
    observation."""

    def __init__(self, *, head: str = OBSERVED_HEAD, drift_head: str | None = None) -> None:
        self.head = head
        self.drift_head = drift_head
        self._rev_parse_calls = 0

    def run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        if command == ["git", "rev-parse", "HEAD"]:
            self._rev_parse_calls += 1
            head = self.drift_head if self.drift_head is not None and self._rev_parse_calls > 1 else self.head
            return _completed(command, stdout=head + "\n")
        if command[:3] == ["git", "cat-file", "-e"]:
            return _completed(command, returncode=0)
        if command[:3] == ["git", "merge-base", "--is-ancestor"]:
            return _completed(command, returncode=0)
        if command[:3] == ["git", "status", "--porcelain"]:
            return _completed(command, stdout="")
        if command[:4] == ["git", "check-ignore", "-q", "--"]:
            return _completed(command, returncode=0)
        raise AssertionError(f"unexpected git command: {command}")


def _fake_opencode_shim(tmp_path: Path) -> Path:
    """A deterministic fake ``opencode.cmd`` that only serves the two local
    inspection commands; an accidental ``opencode run`` invocation exits
    nonzero."""
    fake_dir = tmp_path / "fake-opencode"
    fake_dir.mkdir()
    fake_impl = fake_dir / "fake_opencode.py"
    entry = json.dumps(FAKE_CATALOG_ENTRY, ensure_ascii=False)
    fake_impl.write_text(
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "if args == ['--version']:\n"
        "    print('1.18.10')\n"
        "elif args[:2] == ['models', 'opencode-go']:\n"
        f"    print({entry!r})\n"
        "elif args and args[0] == 'run':\n"
        "    raise SystemExit(91)\n"
        "else:\n"
        "    raise SystemExit(92)\n",
        encoding="utf-8",
    )
    fake_launcher = fake_dir / "opencode.cmd"
    fake_launcher.write_text(f'@"{sys.executable}" "%~dp0fake_opencode.py" %*\n', encoding="utf-8")
    return fake_dir


def test_route_capture_cli_runs_only_local_inspection_commands(tmp_path, monkeypatch, capsys) -> None:
    shim_dir = _fake_opencode_shim(tmp_path)
    monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(adapter, "OPERATOR_STORAGE", tmp_path / "operator")
    target = tmp_path / "operator" / "route-evidence" / "quixbugs-route-evidence-v1-cli-test.json"
    rc = adapter.main([
        "route-capture",
        "--runtime-model-id", MODEL,
        "--variant", "max",
        "--account-status", "ACTIVE",
        "--subscription-entitlement-confirmed",
        "--entitlement-evidence-reference", "operator-account-observation-20260803-001",
        "--billing-route-assertion", "SUBSCRIPTION",
        "--output", str(target),
    ])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    result = json.loads(captured.out)
    assert result["captured"] is True
    assert result["run_invoked"] is False
    assert result["opencode_version"] == "1.18.10"
    assert result["catalog_entry_id"] == "deepseek-v4-flash"
    evidence = json.loads(target.read_text(encoding="utf-8"))
    assert evidence["schema_version"] == adapter.ROUTE_EVIDENCE_SCHEMA_VERSION
    # The expected fingerprint is derived from the exact catalog fixture the
    # fake shim serves.
    assert evidence["catalog_fingerprint"] == transport.catalog_entry_fingerprint(FAKE_CATALOG_ENTRY)
    assert evidence["catalog_fingerprint"] == result["catalog_fingerprint"]
    assert evidence["runtime_model_id"] == MODEL
    runner.validate_raw_route_evidence(evidence, {"expected_account_status": "ACTIVE"})
    # The fake shim proves no ``opencode run`` was ever constructed: an
    # accidental invocation would have exited 91 and blocked the capture.
    assert result["mode"] == "route-capture"


def test_operator_bundle_to_route_preflight_only_handoff(tmp_path, monkeypatch, capsys) -> None:
    """The accepted route-evidence file feeds the operator bundle, the
    materialized authorization + adapter configuration are bound to the
    independently observed clean Git HEAD, adapter validation passes, and the
    existing route-preflight-only command completes every pre-provider gate
    with zero provider processes."""
    monkeypatch.setattr(adapter, "_git", FakeGit().run)
    monkeypatch.setattr(runner, "real_git_state", lambda commit: _clean_git_state(commit))
    monkeypatch.setattr(adapter, "OPERATOR_STORAGE", tmp_path / "operator")

    evidence = {
        "schema_version": adapter.ROUTE_EVIDENCE_SCHEMA_VERSION,
        "provider": "OpenCode Go",
        "model": "deepseek-v4-flash",
        "variant": "max",
        "protocol": "1.3",
        "opencode_version": "1.18.10",
        "catalog_fingerprint": "e" * 64,
        "runtime_model_id": MODEL,
        "billing_route": "SUBSCRIPTION",
        "subscription_entitlement_confirmed": True,
        "account_status": "ACTIVE",
        "active_model_status": "ACTIVE",
        "variant_available": True,
        "input_price": 0.5,
        "output_price": 1.5,
        "provider_reported_cost": 0.0,
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
    evidence_path = tmp_path / "operator" / "route-evidence" / "evidence.json"
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")

    output_root = tmp_path / "operator" / "attempts" / ATTEMPT
    bundle_root = tmp_path / "operator" / "bundles" / ATTEMPT
    boundary = adapter.common_operator_boundary([sys.executable, tmp_path])

    rc = adapter.main([
        "operator-bundle",
        "--route-evidence-json", str(evidence_path),
        "--operator-authorization-id", AUTH_ID,
        "--attempt-identity", ATTEMPT,
        "--output", str(output_root),
        "--valid-until", _later_iso(),
        "--entitlement-evidence-reference", "operator-account-observation-20260803-001",
        "--python-executable", sys.executable,
        "--working-directory", str(tmp_path),
        "--operator-boundary-root", str(boundary),
        "--bundle-root", str(bundle_root),
    ])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    bundle = json.loads(captured.out)
    assert bundle["materialized"] is True
    assert bundle["execution_commit"] == OBSERVED_HEAD
    assert bundle["independently_observed_head"] == OBSERVED_HEAD
    assert bundle["execution_commit"] != adapter.TASK_BASELINE
    assert bundle["provider_processes_created"] == 0
    authorization_path = Path(bundle["authorization_path"])
    configuration_path = Path(bundle["configuration_path"])
    assert authorization_path.is_file() and configuration_path.is_file()
    authorization = json.loads(authorization_path.read_text(encoding="utf-8"))
    configuration = json.loads(configuration_path.read_text(encoding="utf-8"))
    assert authorization["accepted_campaign_commit"] == OBSERVED_HEAD
    assert configuration["execution_commit"] == OBSERVED_HEAD

    rc = adapter.main([
        "adapter-validate",
        "--adapter-config", str(configuration_path),
        "--authorization", str(authorization_path),
        "--route-evidence-json", str(evidence_path),
    ])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    validated = json.loads(captured.out)
    assert validated["valid"] is True
    assert validated["binding_checked"] is True
    assert validated["execution_commit"] == OBSERVED_HEAD

    launches: list = []
    real_popen = subprocess.Popen

    def spy_popen(command, **kwargs):
        launches.append(command)
        return real_popen(command, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy_popen)
    rc = adapter.main([
        "route-preflight-only",
        "--authorization", str(authorization_path),
        "--route-evidence-json", str(evidence_path),
        "--adapter-config", str(configuration_path),
        "--output", str(output_root),
    ])
    assert rc == 0
    assert launches == []
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "route-preflight-only"
    assert result["preflight"]["passed"] is True
    assert result["provider_processes_created"] == 0
    assert result["preflight"]["route_observation"]["execution_commit"] == OBSERVED_HEAD


def test_operator_bundle_rejects_execution_head_drift_at_cli(tmp_path, monkeypatch, capsys) -> None:
    """If the Git HEAD drifts between observation and materialization the
    bundle fails closed and creates neither active artifact."""
    monkeypatch.setattr(adapter, "_git", FakeGit(drift_head="c" * 40).run)
    monkeypatch.setattr(adapter, "OPERATOR_STORAGE", tmp_path / "operator")
    evidence = {
        "schema_version": adapter.ROUTE_EVIDENCE_SCHEMA_VERSION,
        "provider": "OpenCode Go",
        "model": "deepseek-v4-flash",
        "variant": "max",
        "protocol": "1.3",
        "opencode_version": "1.18.10",
        "catalog_fingerprint": "e" * 64,
        "runtime_model_id": MODEL,
        "billing_route": "SUBSCRIPTION",
        "subscription_entitlement_confirmed": True,
        "account_status": "ACTIVE",
        "active_model_status": "ACTIVE",
        "variant_available": True,
        "input_price": 0.5,
        "output_price": 1.5,
        "provider_reported_cost": 0.0,
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
    evidence_path = tmp_path / "operator" / "route-evidence" / "evidence.json"
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    rc = adapter.main([
        "operator-bundle",
        "--route-evidence-json", str(evidence_path),
        "--operator-authorization-id", AUTH_ID,
        "--attempt-identity", ATTEMPT,
        "--output", str(tmp_path / "operator" / "attempts" / ATTEMPT),
        "--valid-until", _later_iso(),
        "--entitlement-evidence-reference", "operator-account-observation-20260803-001",
        "--python-executable", sys.executable,
        "--working-directory", str(tmp_path),
        "--operator-boundary-root", str(adapter.common_operator_boundary([sys.executable, tmp_path])),
        "--bundle-root", str(tmp_path / "operator" / "bundles" / ATTEMPT),
    ])
    assert rc == 2
    assert "execution-commit drift" in capsys.readouterr().err
    assert not (tmp_path / "operator" / "bundles").exists()
