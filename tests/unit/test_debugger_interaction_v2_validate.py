"""Unit tests for the S1 runner ``--validate-only`` source provenance.

These tests prove (REPAIR PASS 1, Blocker 2):
- ``--validate-only`` passes and reports ``source_commit_sha`` (the live
  ``git rev-parse HEAD`` at runtime, NOT hardcoded) and
  ``experiment_contract_sha256`` in both the ``validation`` block and
  ``run_identity``;
- ``run_identity`` records ``source_commit_sha`` and
  ``experiment_contract_sha256``;
- ``source_commit_sha`` is a 40-hex SHA matching the live repository HEAD;
- ``experiment_contract_sha256`` equals ``contract_sha256`` (no drift).

They mirror the v1 frozen-pilot validate-only pattern
(test_tuned_debugger_pilot.py::test_frozen_tuned_debugger_pilot_validate_only).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.debugger_interaction_v2 import runner

RUNNER_SCRIPT = (
    REPO_ROOT / "experiments" / "debugger_interaction_v2" / "runner.py"
)

# Frozen values (must not drift).
EXPECTED_CONTRACT_SHA256 = (
    "2cbed58e74c113a93e9d3b82de03abfb09a0dd98acd1a5476808b4f83d6d34db"
)
EXPECTED_BASE_REVISION = "c03e6d358207e414f1eca0bb1891e29f1db0e242"
EXPECTED_TASK_ID = "curated-off-by-one-002"

_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _live_git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return completed.stdout.strip()


def test_validate_only_passes_and_reports_source_provenance() -> None:
    """``--validate-only`` must pass and report source-commit + contract SHA
    provenance in both the validation block and run_identity."""

    completed = subprocess.run(
        [sys.executable, str(RUNNER_SCRIPT), "--validate-only"],
        cwd=str(REPO_ROOT),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(completed.stdout)

    assert payload["status"] == "PASS"

    validation = payload["validation"]
    assert validation["validated"] is True
    assert validation["contract_sha256"] == EXPECTED_CONTRACT_SHA256
    # experiment_contract_sha256 must equal contract_sha256 (no drift).
    assert validation["experiment_contract_sha256"] == validation["contract_sha256"]
    # source_commit_sha is the live HEAD at runtime, a 40-hex SHA.
    source_sha = validation["source_commit_sha"]
    assert isinstance(source_sha, str)
    assert _COMMIT_SHA_RE.match(source_sha), \
        f"source_commit_sha is not a 40-hex SHA: {source_sha!r}"
    assert source_sha == _live_git_head()

    identity = payload["run_identity"]
    assert identity["schema_version"] == "debugger-interaction-v2-identity"
    assert identity["experiment_id"] == "debugger-interaction-v2"
    # Run identity carries the same source provenance.
    assert identity["source_commit_sha"] == source_sha
    assert identity["experiment_contract_sha256"] == validation["contract_sha256"]
    # Frozen model/task/treatment identity unchanged.
    assert identity["model_condition"] == "RAW_BASE"
    assert identity["adapter_applied"] is False
    assert identity["adapter_path"] is None
    assert identity["rag_enabled"] is False
    assert identity["base_repository"] == "Qwen/Qwen2.5-Coder-7B-Instruct"
    assert identity["base_revision"] == EXPECTED_BASE_REVISION
    assert identity["task_id"] == EXPECTED_TASK_ID


def test_run_identity_records_source_commit_and_contract_sha() -> None:
    """``_run_identity`` must record ``source_commit_sha`` and
    ``experiment_contract_sha256`` matching the live HEAD and the contract
    SHA computed by ``_validate_contract``."""

    contract = runner._load_contract()
    identity = runner._run_identity(contract)

    assert "source_commit_sha" in identity
    assert "experiment_contract_sha256" in identity

    source_sha = identity["source_commit_sha"]
    assert isinstance(source_sha, str)
    assert _COMMIT_SHA_RE.match(source_sha), \
        f"source_commit_sha is not a 40-hex SHA: {source_sha!r}"
    assert source_sha == _live_git_head()

    # The run-identity contract SHA must equal the validate-only contract SHA.
    validation = runner._validate_contract(contract)
    assert identity["experiment_contract_sha256"] == validation["contract_sha256"]
    assert identity["experiment_contract_sha256"] == EXPECTED_CONTRACT_SHA256