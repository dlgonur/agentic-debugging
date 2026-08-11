"""R4 integration test — FakeTransport full pipeline (amendment 11).

generated T
-> BUGGY behavioral FAIL (structured)
-> exact same T
-> R_fix_C applied in a separate disposable workspace
-> FIXED PASS
-> independent EvaluationVerifier RESOLVED
-> canonical fixture unchanged, workspaces cleaned.

Also covers the first-causal-boundary paths (buggy passes the test; verifier
gate) and the offline CLI smoke.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.model_generated_test_probe_r4 import probe as r4_probe


def _contract():
    return r4_probe._load_contract()


class TestFullPipeline:
    def test_offline_pipeline_reaches_r4_pass(self, tmp_path):
        contract = _contract()
        r4_probe._validate_contract(contract)
        evidence = r4_probe.run_probe(
            contract,
            r4_probe._build_offline_transport(),
            model_name="offline-fake-transport",
            output_dir=tmp_path / "evidence",
            case_parent=tmp_path,
        )

        # Summary gate.
        assert evidence["summary"]["r4_pass"] is True
        assert evidence["summary"]["stop_reason"] == "completed"
        assert evidence["summary"]["generated_test_froze"] is True
        assert evidence["summary"]["buggy_failed_frozen_test"] is True
        assert evidence["summary"]["fixed_code_passed_frozen_test"] is True
        assert evidence["summary"]["same_test_identity"] is True
        assert evidence["summary"]["verifier_resolved"] is True
        assert evidence["first_causal_boundary"] is None

        # Anti-leakage on the final rendered prompt.
        assert evidence["anti_leakage"]["passed"] is True

        # Identities: T_raw / T_parsed / T_written.
        frozen = evidence["test_generation"]["frozen_test"]
        framing = evidence["framing"]
        assert len(evidence["test_generation"]["attempts"]) == 1
        attempt = evidence["test_generation"]["attempts"][0]
        assert attempt["attempt_index"] == 0
        assert framing["t_raw_sha256"] == frozen["raw_response_sha256"]
        assert framing["t_parsed_sha256"] == frozen["sha256"]
        assert framing["t_written_sha256"] == frozen["sha256"]
        assert "no newline translation" in framing["relation"]

        # BUGGY structured failure.
        buggy = evidence["buggy_run"]
        assert buggy["status"] == "FAIL"
        assert buggy["valid_buggy_failure"] is True
        assert buggy["compiled"] is True
        assert buggy["collected"] == 1
        assert buggy["collect_error"] is False
        assert buggy["counts"]["failed"] == 1
        assert buggy["counts"]["errors"] == 0
        assert buggy["assertion_attributed"] is True
        assert buggy["infrastructure_markers"] == []
        assert buggy["workspace_cleaned"] is True

        # FIXED: strictly separate workspace, same T_written bytes.
        fixed = evidence["fixed_run"]
        assert fixed["written_test_sha256"] == buggy["written_test_sha256"]
        assert fixed["status"] == "PASS"
        assert fixed["executed"] is True
        assert fixed["patch_applied"] is True
        assert fixed["patch_error"] is None
        assert fixed["workspace_cleaned"] is True

        # R_fix identity asserted.
        assert evidence["r3_fix_identity"]["r_fix_b_sha256"] == (
            "831b1c2bc347c9812296de5ddb7ebac5f6f414bbd6512561b4cb29066e6e2c76"
        )
        assert evidence["r3_fix_identity"]["r_fix_c_sha256"] == (
            "8c051faa605d9cf736540301e204639870408b288ab1ceb8348845afc674b990"
        )

        # Independent verifier: frozen contract.
        verifier = evidence["verifier"]
        assert verifier["executed"] is True
        assert verifier["status"] == "COMPLETED"
        assert verifier["outcome"] == "RESOLVED"
        assert verifier["f2p_total"] == 1 and verifier["f2p_passed"] == 1
        assert verifier["p2p_total"] == 2 and verifier["p2p_passed"] == 2
        assert verifier["workspace_lifecycle"] == "CLEANED"
        assert verifier["canonical_fixture_unchanged"] is True
        assert verifier["f2p_records"] == ["PASS"]
        assert verifier["p2p_records"] == ["PASS", "PASS"]

        # Evidence file written.
        evidence_path = tmp_path / "evidence" / "evidence.json"
        assert evidence_path.is_file()
        reloaded = json.loads(evidence_path.read_text(encoding="utf-8"))
        assert reloaded["summary"]["r4_pass"] is True

    def test_buggy_passes_test_leaves_r4_open(self, tmp_path):
        # A generated test that does NOT encode the defect (passes buggy).
        response = (
            "```python\n"
            "from recent_window import recent_window\n\n\n"
            "def test_ordinary_window() -> None:\n"
            "    assert recent_window([10, 20, 30, 40], 2) == [30, 40]\n"
            "```\n"
        )
        from experiments.debugger_interaction_v2_r3.transport import FakeTransport

        contract = _contract()
        evidence = r4_probe.run_probe(
            contract,
            FakeTransport((response,)),
            model_name="offline-fake-transport",
            output_dir=tmp_path / "evidence2",
            case_parent=tmp_path,
        )
        assert evidence["summary"]["r4_pass"] is False
        assert evidence["summary"]["generated_test_did_not_encode_defect"] is True
        assert evidence["first_causal_boundary"]["stage"] == "buggy_not_valid_failure"
        assert evidence["summary"]["stop_reason"] == "open_buggy_not_valid_failure"
        assert evidence["fixed_run"] is None
        assert evidence["verifier"]["executed"] is False

    def test_transport_failure_leaves_r4_open(self, tmp_path):
        from experiments.debugger_interaction_v2_r3.transport import FailingTransport

        contract = _contract()
        evidence = r4_probe.run_probe(
            contract,
            FailingTransport("generation_error"),
            model_name="offline-fake-transport",
            output_dir=tmp_path / "evidence3",
            case_parent=tmp_path,
        )
        assert evidence["summary"]["r4_pass"] is False
        assert evidence["first_causal_boundary"]["stage"] == "transport_failure"
        assert evidence["test_generation"]["stop_reason"] == "transport_failure"

    def test_offline_cli_smoke(self, tmp_path, capsys):
        import subprocess
        import sys as _sys

        out = tmp_path / "cli-evidence"
        completed = subprocess.run(
            [
                _sys.executable,
                str(REPO_ROOT / "experiments" / "model_generated_test_probe_r4" / "probe.py"),
                "--run-offline",
                "--output-dir",
                str(out),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=300, check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        payload = json.loads(completed.stdout)
        assert payload["status"] == "COMPLETE_OFFLINE"
        assert payload["summary"]["r4_pass"] is True
        assert (out / "evidence.json").is_file()
