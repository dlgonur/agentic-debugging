"""Repair-9 DEVQUAL readiness, provenance, and no-provider regressions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_debugger.application.worker_scenarios import (
    PreModelSetupFailure,
    ScenarioInputError,
)
from agentic_debugger.swerebench.devqual import (
    DEVQUAL_EXTERNAL_ROOT,
    DEVQUAL_FROZEN_DIR,
    DEVQUAL_PREFLIGHT_ROOT,
)
from agentic_debugger.swerebench.hashing import sha256_file
from agentic_debugger.swerebench.preflight import (
    load_preflight_bundle,
    write_preflight_bundle,
)
from agentic_debugger.swerebench.schema import classify_execution_result
from scripts.gpt_oss_swerebench_v2_devqual10 import build_parser
from scripts.gpt_oss_swerebench_v2_pilot10 import (
    authorization_evidence_path,
    provider_execution_truth,
)


def _records() -> list[dict]:
    return [
        {
            "instance_id": f"repo-{index}",
            "authorization_status": "ready-for-authorized-execution",
            "verifier_baseline_valid": True,
        }
        for index in range(10)
    ]


def test_devqual_defaults_keep_readiness_and_campaign_roots_distinct():
    preflight = build_parser().parse_args(["preflight"])
    authorize = build_parser().parse_args(["authorize", "--config-root", "C:/external/config"])
    assert Path(preflight.output_summary).parent == DEVQUAL_PREFLIGHT_ROOT
    assert Path(preflight.external_root) == DEVQUAL_EXTERNAL_ROOT
    assert Path(preflight.output_summary).resolve() != Path(preflight.external_root).resolve()
    assert authorize.authorization_output is None


def test_external_preflight_bundle_persists_exact_ten_records_and_binds_them(tmp_path: Path):
    root = tmp_path / "readiness"
    records = _records()
    summary = write_preflight_bundle(
        root,
        summary={"experiment_id": "devqual", "n": 10, "ready": 10, "invalid": [], "records": []},
        records=records,
    )
    assert len(list((root / "records").glob("*.json"))) == 10
    loaded_summary, loaded, fingerprint = load_preflight_bundle(
        root / "summary.json",
        record_dir=root / "records",
        expected_instance_ids=[record["instance_id"] for record in records],
    )
    assert len(loaded) == 10
    assert fingerprint == summary["evidence_fingerprint"]
    assert loaded_summary["record_files"][0]["sha256"] == sha256_file(
        root / "records" / "repo-0.json"
    )

    (root / "records" / "repo-0.json").write_text(
        json.dumps({**records[0], "verifier_baseline_valid": False}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="missing or changed"):
        load_preflight_bundle(
            root / "summary.json",
            record_dir=root / "records",
            expected_instance_ids=[record["instance_id"] for record in records],
        )


def test_authorization_evidence_rejects_repository_paths(tmp_path: Path):
    with pytest.raises(ValueError, match="outside the repository"):
        authorization_evidence_path(
            tmp_path / "config",
            tmp_path / "authorization.json",
            project=tmp_path,
        )


def test_provider_inference_truth_uses_durable_transport_attempts():
    truth = provider_execution_truth(
        [
            {"runtime": {"transport_attempts": 0}},
            {"runtime": {"transport_attempts": 2}},
            {"runtime": {"transport_attempts": None}},
        ]
    )
    assert truth["provider_inference_started"] is True
    assert truth["tasks_with_transport_attempts"] == 1
    assert truth["transport_attempts"] == 2
    assert provider_execution_truth([{"runtime": {"transport_attempts": 0}}])["provider_inference_started"] is False


def test_pre_model_setup_failure_is_not_an_admissible_model_result():
    assert issubclass(PreModelSetupFailure, ScenarioInputError)
    assert classify_execution_result(
        controller_completed=False,
        candidate_produced=False,
        verifier_ran=False,
        verifier_resolved=False,
        verifier_infrastructure_valid=True,
        runtime_infrastructure_invalid=True,
    ) == "infrastructure_invalid"
