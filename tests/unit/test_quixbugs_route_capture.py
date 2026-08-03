"""Operator route-capture tests: deterministic catalog-entry fingerprinting,
exact selected-entry matching, strict route-evidence schema production, and
the proof that capture never constructs or invokes ``opencode run``."""
from __future__ import annotations

import json
import re
import subprocess
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
from scripts import opencode_protocol_transport as transport


MODEL = "opencode-go/deepseek-v4-flash"
VARIANT = "max"
ENTRY = {
    "id": "deepseek-v4-flash",
    "providerID": "opencode-go",
    "status": "active",
    "cost": {"input": 0.5, "output": 1.5, "cache": {"read": 0.25, "write": 0.25}},
    "variants": {"max": {"reasoningEffort": "max"}},
}
VERSION = "1.18.10"


def _now_iso(reference: datetime | None = None) -> str:
    value = reference if reference is not None else datetime.now(timezone.utc)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _completed(command: list[str], stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, "")


def _catalog_output(entries: list[dict]) -> str:
    return "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n"


def _monkeypatch_capture(monkeypatch: pytest.MonkeyPatch, catalog: str) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs):
        calls.append(command)
        if command == ["opencode.cmd", "--version"]:
            return _completed(command, stdout=VERSION + "\n")
        if command[1:3] == ["models", "opencode-go"]:
            return _completed(command, stdout=catalog)
        raise AssertionError(f"unexpected OpenCode command during capture: {command}")

    monkeypatch.setattr(transport.subprocess, "run", fake_run)
    monkeypatch.setattr(adapter.subprocess, "run", fake_run)
    monkeypatch.setattr(transport.shutil, "which", lambda name: r"C:\fake\opencode.cmd")
    return calls


def _capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, catalog: str | None = None, reference: datetime | None = None, **kwargs) -> dict:
    monkeypatch.setattr(adapter, "OPERATOR_STORAGE", tmp_path)
    defaults = {
        "runtime_model_id": MODEL,
        "variant": VARIANT,
        "account_status": "ACTIVE",
        "subscription_entitlement_confirmed": True,
        "entitlement_evidence_reference": "operator-account-observation-20260803-001",
        "billing_route_assertion": "SUBSCRIPTION",
        "output": tmp_path / "route-evidence" / "evidence.json",
    }
    defaults.update(kwargs)
    calls = _monkeypatch_capture(monkeypatch, catalog if catalog is not None else _catalog_output([ENTRY]))
    result = adapter.run_route_capture(now=reference if reference is not None else datetime.now(timezone.utc), **defaults)
    return {"result": result, "calls": calls}


# ---- deterministic catalog-entry fingerprinting --------------------------------


def test_catalog_entry_fingerprint_is_deterministic_and_canonical() -> None:
    first = transport.catalog_entry_fingerprint(ENTRY)
    assert first == transport.catalog_entry_fingerprint(dict(ENTRY))
    reordered = {
        "variants": {"max": {"reasoningEffort": "max"}},
        "providerID": "opencode-go",
        "status": "active",
        "cost": {"cache": {"read": 0.25, "write": 0.25}, "output": 1.5, "input": 0.5},
        "id": "deepseek-v4-flash",
    }
    assert transport.catalog_entry_fingerprint(reordered) == first
    mutated = dict(ENTRY)
    mutated["status"] = "inactive"
    assert transport.catalog_entry_fingerprint(mutated) != first
    assert len(first) == 64 and re.fullmatch(r"[0-9a-f]{64}", first)


def test_catalog_entry_fingerprint_is_sha256_of_canonical_serialization() -> None:
    import hashlib

    canonical = json.dumps(ENTRY, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert transport.catalog_entry_fingerprint(ENTRY) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert adapter.transport.catalog_entry_fingerprint is transport.catalog_entry_fingerprint


def test_catalog_entry_fingerprint_round_trips_through_catalog_json() -> None:
    parsed = json.loads(_catalog_output([ENTRY]).strip().splitlines()[0])
    assert transport.catalog_entry_fingerprint(parsed) == transport.catalog_entry_fingerprint(ENTRY)


# ---- exact selected-entry matching ---------------------------------------------


def test_route_capture_selects_one_exact_active_entry(tmp_path, monkeypatch) -> None:
    captured = _capture(tmp_path, monkeypatch)
    assert captured["result"]["captured"] is True
    assert captured["result"]["catalog_entry_id"] == "deepseek-v4-flash"
    assert captured["result"]["catalog_fingerprint"] == transport.catalog_entry_fingerprint(ENTRY)


def test_route_capture_rejects_duplicate_catalog_entries(tmp_path, monkeypatch) -> None:
    duplicate = [ENTRY, dict(ENTRY)]
    monkeypatch.setattr(adapter, "OPERATOR_STORAGE", tmp_path)
    calls = _monkeypatch_capture(monkeypatch, _catalog_output(duplicate))
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="not uniquely present"):
        adapter.run_route_capture(
            MODEL, VARIANT,
            account_status="ACTIVE", subscription_entitlement_confirmed=True,
            entitlement_evidence_reference="ref-001", billing_route_assertion="SUBSCRIPTION",
            output=tmp_path / "evidence.json",
        )
    assert not (tmp_path / "evidence.json").exists()


def test_route_capture_rejects_inactive_entry(tmp_path, monkeypatch) -> None:
    inactive = dict(ENTRY, status="inactive")
    monkeypatch.setattr(adapter, "OPERATOR_STORAGE", tmp_path)
    _monkeypatch_capture(monkeypatch, _catalog_output([inactive]))
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="not active or has incomplete pricing"):
        adapter.run_route_capture(
            MODEL, VARIANT,
            account_status="ACTIVE", subscription_entitlement_confirmed=True,
            entitlement_evidence_reference="ref-001", billing_route_assertion="SUBSCRIPTION",
            output=tmp_path / "evidence.json",
        )
    assert not (tmp_path / "evidence.json").exists()


def test_route_capture_rejects_malformed_pricing_metadata(tmp_path, monkeypatch) -> None:
    malformed = dict(ENTRY, cost={"input": 0.5, "output": None, "cache": {"read": 0.25, "write": 0.25}})
    monkeypatch.setattr(adapter, "OPERATOR_STORAGE", tmp_path)
    _monkeypatch_capture(monkeypatch, _catalog_output([malformed]))
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="malformed pricing"):
        adapter.run_route_capture(
            MODEL, VARIANT,
            account_status="ACTIVE", subscription_entitlement_confirmed=True,
            entitlement_evidence_reference="ref-001", billing_route_assertion="SUBSCRIPTION",
            output=tmp_path / "evidence.json",
        )


def test_route_capture_rejects_missing_variant(tmp_path, monkeypatch) -> None:
    missing_variant = dict(ENTRY, variants={"low": {"reasoningEffort": "low"}})
    monkeypatch.setattr(adapter, "OPERATOR_STORAGE", tmp_path)
    _monkeypatch_capture(monkeypatch, _catalog_output([missing_variant]))
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="variant is unavailable"):
        adapter.run_route_capture(
            MODEL, VARIANT,
            account_status="ACTIVE", subscription_entitlement_confirmed=True,
            entitlement_evidence_reference="ref-001", billing_route_assertion="SUBSCRIPTION",
            output=tmp_path / "evidence.json",
        )


def test_route_capture_rejects_historical_free_route_identity(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(adapter, "OPERATOR_STORAGE", tmp_path)
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="historical OpenCode Zen"):
        adapter.run_route_capture(
            adapter.HISTORICAL_ZEN_MODEL_ID, VARIANT,
            account_status="ACTIVE", subscription_entitlement_confirmed=True,
            entitlement_evidence_reference="ref-001", billing_route_assertion="SUBSCRIPTION",
            output=tmp_path / "evidence.json",
        )


def test_route_capture_rejects_unqualified_or_other_family_identity(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(adapter, "OPERATOR_STORAGE", tmp_path)
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="catalog-qualified"):
        adapter.run_route_capture(
            "deepseek-v4-flash", VARIANT,
            account_status="ACTIVE", subscription_entitlement_confirmed=True,
            entitlement_evidence_reference="ref-001", billing_route_assertion="SUBSCRIPTION",
            output=tmp_path / "evidence.json",
        )
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="opencode-go/ catalog-qualified provider prefix"):
        adapter.run_route_capture(
            "opencode/another-model", VARIANT,
            account_status="ACTIVE", subscription_entitlement_confirmed=True,
            entitlement_evidence_reference="ref-001", billing_route_assertion="SUBSCRIPTION",
            output=tmp_path / "evidence.json",
        )
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="not the frozen campaign model family"):
        adapter.run_route_capture(
            "opencode-go/another-model", VARIANT,
            account_status="ACTIVE", subscription_entitlement_confirmed=True,
            entitlement_evidence_reference="ref-001", billing_route_assertion="SUBSCRIPTION",
            output=tmp_path / "evidence.json",
        )


# ---- route-evidence schema production ------------------------------------------


def test_route_capture_produces_strict_evidence_accepted_by_live_runner(tmp_path, monkeypatch) -> None:
    reference = datetime(2026, 8, 3, 10, 0, 0, tzinfo=timezone.utc)
    captured = _capture(tmp_path, monkeypatch, reference=reference)
    target = tmp_path / "route-evidence" / "evidence.json"
    evidence = json.loads(target.read_text(encoding="utf-8"))
    assert set(evidence) == set(runner.RAW_ROUTE_EVIDENCE_SCHEMA) | {"schema_version"}
    assert evidence["schema_version"] == adapter.ROUTE_EVIDENCE_SCHEMA_VERSION
    assert evidence["runtime_model_id"] == MODEL
    assert evidence["model"] == pilot.SUBSCRIPTION_ROUTE_MODEL
    assert evidence["provider"] == pilot.SUBSCRIPTION_ROUTE_PROVIDER
    assert evidence["variant"] == VARIANT
    assert evidence["protocol"] == runner.LIVE_PROTOCOL_VERSION
    assert evidence["opencode_version"] == VERSION
    assert evidence["catalog_fingerprint"] == transport.catalog_entry_fingerprint(ENTRY)
    assert evidence["billing_route"] == "SUBSCRIPTION"
    assert evidence["subscription_entitlement_confirmed"] is True
    assert evidence["account_status"] == "ACTIVE"
    assert evidence["active_model_status"] == "ACTIVE"
    assert evidence["variant_available"] is True
    assert evidence["input_price"] == 0.5
    assert evidence["output_price"] == 1.5
    assert evidence["provider_reported_cost"] == 0.0
    assert evidence["observed_at"] == _now_iso(reference)
    for flag in ("zen_used", "free_tier_used", "ollama_used", "paid_fallback_used",
                 "alternate_provider_used", "metered_fallback_used", "paid_overage_used",
                 "per_call_billing_used", "model_substitution_observed"):
        assert evidence[flag] is False
    validated = runner.validate_raw_route_evidence(evidence, {"expected_account_status": "ACTIVE"}, now=reference)
    assert validated["catalog_fingerprint"] == evidence["catalog_fingerprint"]
    capture_record = json.loads(target.with_suffix(".json.capture-record.json").read_text(encoding="utf-8"))
    assert capture_record["provider_contact_proof"]["run_invoked"] is False
    assert capture_record["provider_contact_proof"]["model_requests"] == 0


def test_route_capture_requires_operator_supplied_assertions(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(adapter, "OPERATOR_STORAGE", tmp_path)
    _monkeypatch_capture(monkeypatch, _catalog_output([ENTRY]))
    base = dict(
        runtime_model_id=MODEL, variant=VARIANT,
        subscription_entitlement_confirmed=True,
        entitlement_evidence_reference="ref-001", billing_route_assertion="SUBSCRIPTION",
        output=tmp_path / "evidence.json",
    )
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="account status"):
        adapter.run_route_capture(account_status="", **base)
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="entitlement"):
        adapter.run_route_capture(account_status="ACTIVE", **{**base, "subscription_entitlement_confirmed": False})
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="evidence reference"):
        adapter.run_route_capture(account_status="ACTIVE", **{**base, "entitlement_evidence_reference": " "})
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="billing-route assertion"):
        adapter.run_route_capture(account_status="ACTIVE", **{**base, "billing_route_assertion": "ZEN"})
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="placeholder"):
        adapter.run_route_capture(**{**base, "account_status": "<expected account status>"})
    assert not (tmp_path / "evidence.json").exists()


def test_route_capture_create_once_and_operator_storage_boundary(tmp_path, monkeypatch) -> None:
    storage = tmp_path / "operator"
    storage.mkdir()
    existing = storage / "existing.json"
    existing.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(adapter, "OPERATOR_STORAGE", storage)
    _monkeypatch_capture(monkeypatch, _catalog_output([ENTRY]))
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="already exists"):
        adapter.run_route_capture(
            MODEL, VARIANT,
            account_status="ACTIVE", subscription_entitlement_confirmed=True,
            entitlement_evidence_reference="ref-001", billing_route_assertion="SUBSCRIPTION",
            output=existing,
        )
    with pytest.raises(adapter.OpenCodeGoAdapterError, match="inside the ignored operator storage"):
        adapter.run_route_capture(
            MODEL, VARIANT,
            account_status="ACTIVE", subscription_entitlement_confirmed=True,
            entitlement_evidence_reference="ref-001", billing_route_assertion="SUBSCRIPTION",
            output=tmp_path / "outside.json",
        )


def test_route_capture_evidence_contains_no_credential_shaped_content(tmp_path, monkeypatch) -> None:
    captured = _capture(tmp_path, monkeypatch)
    payload = (tmp_path / "route-evidence" / "evidence.json").read_text(encoding="utf-8").lower()
    for marker in ("api_key", "token", "authorization:", "cookie", "password", "bearer "):
        assert marker not in payload
    assert captured["result"]["run_invoked"] is False


# ---- proof that capture never constructs or invokes ``opencode run`` -----------


def test_route_capture_never_constructs_or_invokes_opencode_run(tmp_path, monkeypatch) -> None:
    captured = _capture(tmp_path, monkeypatch)
    calls = captured["calls"]
    # The actual command inventory is the proof: the capture runs exactly the
    # two local inspection commands and never constructs an ``opencode run``
    # invocation (no command contains the ``run`` subcommand).
    assert calls == [["opencode.cmd", "--version"], ["opencode.cmd", "models", "opencode-go", "--verbose", "--pure"]]
    assert all(command[0] == "opencode.cmd" for command in calls)
    assert all("run" not in command for command in calls)
    assert captured["result"]["run_invoked"] is False
    assert captured["result"]["model_requests"] == 0
