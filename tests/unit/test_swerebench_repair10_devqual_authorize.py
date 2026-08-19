"""Repair-10 DEVQUAL authorization contract-shape regressions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import scripts.gpt_oss_swerebench_v2_devqual10 as devqual
from agentic_debugger.swerebench.devqual import DEVQUAL_EXPERIMENT_ID, load_devqual_contract
from scripts.gpt_oss_swerebench_v2_devqual10 import _authorize, build_parser


HISTORICAL_V1_STATUS = "diagnostic / infrastructure-invalid, not a capability score"


def _authorize_argv(tmp_path: Path) -> list[str]:
    return [
        "authorize",
        "--config-root",
        str(tmp_path / "config"),
        "--external-root",
        str(tmp_path / "campaign"),
        "--preflight-summary",
        str(tmp_path / "readiness" / "summary.json"),
        "--authorization-output",
        str(tmp_path / "authorization" / "result.json"),
    ]


def _authorize_args(tmp_path: Path) -> argparse.Namespace:
    return build_parser().parse_args(_authorize_argv(tmp_path))


def _ready_preflight_result() -> dict[str, object]:
    return {
        "ready": True,
        "provider_generation_calls": 0,
        "provider_inference_started": False,
        "tasks_with_transport_attempts": 0,
        "transport_attempts": 0,
        "preflight_evidence_fingerprint": "evidence-fingerprint",
        "profile_metadata": {"configuration_fingerprint": "profile-fingerprint"},
        "checks": {},
        "reasons": [],
    }


def test_authorize_reads_status_from_actual_frozen_historical_v1_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        devqual,
        "run_zero_provider_authorization_preflight",
        lambda **_kwargs: _ready_preflight_result(),
    )

    result = _authorize(_authorize_args(tmp_path))

    assert load_devqual_contract()["historical_v1"]["status"] == HISTORICAL_V1_STATUS
    assert result["historical_v1_status"] == HISTORICAL_V1_STATUS
    assert result["qualification_only"] is True
    assert result["experiment_id"] == DEVQUAL_EXPERIMENT_ID
    assert result["provider_generation_calls"] == 0


def test_authorize_rejects_missing_historical_v1_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    contract = load_devqual_contract()
    contract["historical_v1"] = {}
    monkeypatch.setattr(devqual, "load_devqual_contract", lambda: contract)
    monkeypatch.setattr(
        devqual,
        "run_zero_provider_authorization_preflight",
        lambda **_kwargs: _ready_preflight_result(),
    )

    with pytest.raises(ValueError, match=r"historical_v1\.status"):
        _authorize(_authorize_args(tmp_path))


def test_authorize_command_writes_external_artifact_without_provider_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    preflight = Mock(return_value=_ready_preflight_result())
    monkeypatch.setattr(
        devqual,
        "run_zero_provider_authorization_preflight",
        preflight,
    )
    assert devqual.main(_authorize_argv(tmp_path)) == 0
    preflight.assert_called_once()

    output = tmp_path / "authorization" / "result.json"
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert output.is_file()
    assert not (tmp_path / "campaign").exists()
    assert payload["historical_v1_status"] == HISTORICAL_V1_STATUS
    assert payload["qualification_only"] is True
    assert payload["experiment_id"] == DEVQUAL_EXPERIMENT_ID
    assert payload["provider_inference_started"] is False
    assert payload["tasks_with_transport_attempts"] == 0
    assert payload["transport_attempts"] == 0
    assert payload["provider_generation_calls"] == 0
