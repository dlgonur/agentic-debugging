"""Metric derivation, reporting and CLI contract tests for the Task 9 demo."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_debugger.demo.cli import (
    DEFAULT_ENTRY_POINT,
    RESULTS_FILENAME,
    SUMMARY_FILENAME,
    build_parser,
    render_reproduction,
    write_outputs,
)
from agentic_debugger.demo.report import (
    STANDING_LIMITATIONS,
    UNSUPPORTED_CLAIMS,
    render_summary,
)
from agentic_debugger.demo.runner import (
    NONDETERMINISTIC_RESULT_KEYS,
    RESULTS_SCHEMA_VERSION,
    DemoCaseResult,
    DemoCaseStatus,
    DemoInputError,
    DemoReport,
    LocalizationOutcome,
    RuntimeEvidenceOutcome,
    classify_localization,
    classify_runtime_evidence,
    curated_task_ids,
    deterministic_view,
    environment_record,
    fixture_digest,
    localization_record,
    results_json,
    run_demo,
    source_tree_digest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _case(
    *,
    task_id: str = "curated-none-handling-001",
    policy: str = "static-baseline",
    status: DemoCaseStatus = DemoCaseStatus.COMPLETED,
    final_state: str = "Done",
    verifier_status: str = "COMPLETED",
    outcome: str | None = "RESOLVED",
    localization: str = LocalizationOutcome.CORRECT_TARGET_SYMBOL.value,
    runtime: str = RuntimeEvidenceOutcome.PDB_NOT_USED.value,
    pdb_observations: int = 0,
    model_calls: int = 14,
    tool_errors: tuple[dict[str, str], ...] = (),
    diagnostics: tuple[str, ...] = (),
    replay_valid: bool = True,
    patch_sha256: str = "0" * 64,
    network_attempts: int = 0,
    provider_attempts: int = 0,
) -> DemoCaseResult:
    return DemoCaseResult(
        task_id=task_id,
        policy=policy,
        pdb_policy="disabled" if policy == "static-baseline" else "on_uncertainty",
        status=status,
        controller={
            "final_state": final_state,
            "stop_reason": "done" if final_state == "Done" else "failed",
            "model_calls": model_calls,
            "step_count": model_calls,
            "action_count": 9,
            "tool_call_count": 9,
            "tool_calls": ["run_reproduction"],
            "budget_state": {
                "patch_attempts": 1,
                "test_runs": 3,
                "pdb_observations": pdb_observations,
                "source_observations": 2,
            },
            "states_visited": ["Done", "Patch", "Reproduce", "Understand", "Validate"],
        },
        gate_decisions=(),
        localization={
            "declared": {"file_path": "display_name.py", "symbol": "format_display_name"},
            "oracle_target_files": ["display_name.py"],
            "oracle_target_symbols": ["format_display_name"],
            "patch_changed_files": ["display_name.py"],
            "outcome": localization,
        },
        patch={
            "source": "offline_catalog_reference_repair",
            "attempted": True,
            "applied": True,
            "changed_files": ["display_name.py"],
            "sha256": patch_sha256,
            "line_count": 8,
            "syntax_passed": True,
        },
        runtime_evidence={
            "policy": "disabled",
            "gate_reached": True,
            "gate_allowed": False,
            "gate_reason": "policy_disabled",
            "session_started": False,
            "pdb_action_count": 0,
            "pdb_observation_attempts": pdb_observations,
            "pdb_observations_succeeded": pdb_observations,
            "pdb_observation_names": [],
            "evidence_collected": False,
            "outcome": runtime,
            "diagnosis_change_attributable_to_runtime_evidence": False,
            "attribution_note": "note",
        },
        controller_validation={
            "baseline_failure_reproduced": True,
            "post_patch_f2p_passed": True,
            "designated_regression_passed": True,
            "outcome": "RESOLVED",
            "abort_reason": None,
        },
        verifier={
            "executed": True,
            "status": verifier_status,
            "stop_reason": "completed",
            "outcome": outcome,
            "f2p_total": 1,
            "f2p_passed": 1,
            "p2p_total": 2,
            "p2p_passed": 2,
            "full_suite_status": "PASS",
            "verification_command_count": 10,
            "workspace_lifecycle": "CLEANED",
            "canonical_fixture_unchanged": True,
            "diagnostic": None,
            "note": None,
        },
        trajectory={
            "replay_attempted": True,
            "replay_valid": replay_valid,
            "replay_error": None if replay_valid else "broken",
            "event_count": 37,
            "action_count": 9,
            "observation_count": 9,
            "observation_status_counts": {"ok": 9, "error": 0, "rejected": 0, "timeout": 0},
        },
        tool_errors=tool_errors,
        offline={
            "network_attempts": network_attempts,
            "provider_attempts": provider_attempts,
            "network_targets": [],
            "provider_modules": [],
            "guard_installed": True,
            "guard_scope": "in-process only",
            "canonical_fixture_sha256_before_controller": "a" * 64,
            "canonical_fixture_sha256_after_controller": "a" * 64,
            "canonical_fixture_unchanged_by_controller": True,
        },
        diagnostics=diagnostics,
        wall_clock_ms=1234,
    )


class TestLocalizationScoring:
    def test_correct_claim_with_applied_patch_scores_correct(self) -> None:
        assert (
            classify_localization(
                {"file_path": "a.py", "symbol": "f"}, ["a.py"], True, ["a.py"], ["f"]
            )
            is LocalizationOutcome.CORRECT_TARGET_SYMBOL
        )

    def test_missing_claim_scores_no_localization(self) -> None:
        assert (
            classify_localization(None, [], False, ["a.py"], ["f"])
            is LocalizationOutcome.NO_LOCALIZATION
        )

    def test_wrong_file_claim_scores_wrong_file(self) -> None:
        assert (
            classify_localization(
                {"file_path": "b.py", "symbol": "f"}, ["b.py"], True, ["a.py"], ["f"]
            )
            is LocalizationOutcome.WRONG_FILE
        )

    def test_patch_outside_the_oracle_files_scores_wrong_file(self) -> None:
        assert (
            classify_localization(
                {"file_path": "a.py", "symbol": "f"}, ["other.py"], True, ["a.py"], ["f"]
            )
            is LocalizationOutcome.WRONG_FILE
        )

    def test_wrong_symbol_scores_wrong_location(self) -> None:
        assert (
            classify_localization(
                {"file_path": "a.py", "symbol": "g"}, ["a.py"], True, ["a.py"], ["f"]
            )
            is LocalizationOutcome.WRONG_LOCATION_IN_CORRECT_FILE
        )

    def test_correct_claim_without_a_patch_scores_no_patch(self) -> None:
        assert (
            classify_localization(
                {"file_path": "a.py", "symbol": "f"}, [], False, ["a.py"], ["f"]
            )
            is LocalizationOutcome.NO_PATCH
        )

    def test_record_keeps_every_fact_the_flat_category_cannot_express(self) -> None:
        # Precedence puts the symbol check ahead of the patch check, so a
        # wrong-symbol-and-no-patch case reads WRONG_LOCATION_IN_CORRECT_FILE.
        # The individual booleans must still expose the missing patch.
        record = localization_record(
            {"file_path": "a.py", "symbol": "g"}, [], False, ["a.py"], ["f"]
        )
        assert record["outcome"] == LocalizationOutcome.WRONG_LOCATION_IN_CORRECT_FILE.value
        assert record["patch_applied"] is False
        assert record["claim_file_matches_oracle"] is True
        assert record["claim_symbol_matches_oracle"] is False
        assert record["patch_within_oracle_files"] is None

    def test_record_marks_an_undeclared_claim_explicitly(self) -> None:
        record = localization_record(None, [], False, ["a.py"], ["f"])
        assert record["outcome"] == LocalizationOutcome.NO_LOCALIZATION.value
        assert record["claim_declared"] is False
        assert record["claim_file_matches_oracle"] is None
        assert record["claim_symbol_matches_oracle"] is None

    def test_record_flags_a_patch_outside_the_oracle_files(self) -> None:
        record = localization_record(
            {"file_path": "a.py", "symbol": "f"}, ["other.py"], True, ["a.py"], ["f"]
        )
        assert record["outcome"] == LocalizationOutcome.WRONG_FILE.value
        assert record["patch_within_oracle_files"] is False


class TestRuntimeEvidenceScoring:
    def test_gate_never_reached(self) -> None:
        assert (
            classify_runtime_evidence(
                gate_reached=False, gate_allowed=False, session_started=False,
                evidence_collected=False,
            )
            is RuntimeEvidenceOutcome.PDB_NOT_REACHED
        )

    def test_gate_denied_reports_not_used(self) -> None:
        assert (
            classify_runtime_evidence(
                gate_reached=True, gate_allowed=False, session_started=False,
                evidence_collected=False,
            )
            is RuntimeEvidenceOutcome.PDB_NOT_USED
        )

    def test_allowed_but_failed_session_reports_tool_failure(self) -> None:
        assert (
            classify_runtime_evidence(
                gate_reached=True, gate_allowed=True, session_started=False,
                evidence_collected=False,
            )
            is RuntimeEvidenceOutcome.PDB_TOOL_FAILURE
        )
        assert (
            classify_runtime_evidence(
                gate_reached=True, gate_allowed=True, session_started=True,
                evidence_collected=False,
            )
            is RuntimeEvidenceOutcome.PDB_TOOL_FAILURE
        )

    def test_collected_evidence_never_claims_a_changed_diagnosis(self) -> None:
        outcome = classify_runtime_evidence(
            gate_reached=True, gate_allowed=True, session_started=True,
            evidence_collected=True,
        )
        assert outcome is RuntimeEvidenceOutcome.PDB_EVIDENCE_COLLECTED
        # No category may assert usefulness, confirmation or refutation: nothing
        # in this demonstration evaluates the evidence against the diagnosis.
        vocabulary = " ".join(item.value for item in RuntimeEvidenceOutcome).upper()
        for forbidden in ("USEFUL", "CONFIRM", "REFUT", "CHANGED", "MISREAD"):
            assert forbidden not in vocabulary


class TestReportDocument:
    def _report(self) -> DemoReport:
        return DemoReport(
            environment={"git_head": "abc", "python_version": "3.11.0"},
            cases=(
                _case(),
                _case(
                    policy="pdb-on-uncertainty",
                    runtime=RuntimeEvidenceOutcome.PDB_EVIDENCE_COLLECTED.value,
                    pdb_observations=3,
                    model_calls=22,
                ),
            ),
        )

    def test_document_shape_and_schema_version(self) -> None:
        mapping = self._report().to_mapping()
        assert mapping["schema_version"] == RESULTS_SCHEMA_VERSION
        assert set(mapping) == {
            "schema_version",
            "environment",
            "determinism",
            "aggregates",
            "cases",
            "timing",
        }
        assert set(NONDETERMINISTIC_RESULT_KEYS).issubset(mapping)
        assert [item["case_id"] for item in mapping["timing"]] == [
            case["case_id"] for case in mapping["cases"]
        ]
        # Wall clock must live only in the nondeterministic section.
        assert "wall_clock_ms" not in json.dumps(mapping["cases"])

    def test_aggregates_are_derived_from_case_records(self) -> None:
        aggregates = self._report().aggregates()
        assert aggregates["case_count"] == 2
        assert aggregates["task_count"] == 1
        by_policy = {item["policy"]: item for item in aggregates["by_policy"]}
        assert by_policy["static-baseline"]["pdb_observation_attempts"] == 0
        assert by_policy["pdb-on-uncertainty"]["pdb_observation_attempts"] == 3
        assert by_policy["pdb-on-uncertainty"]["pdb_observations_succeeded"] == 3
        assert by_policy["pdb-on-uncertainty"]["model_calls"] == 22
        assert by_policy["static-baseline"]["verifier_outcomes"] == {"RESOLVED": 1}
        assert by_policy["static-baseline"]["f2p_passed"] == 1
        assert by_policy["static-baseline"]["p2p_total"] == 2

    def test_patch_parity_is_derived_from_the_recorded_digests(self) -> None:
        identical = DemoReport(
            environment={},
            cases=(
                _case(policy="static-baseline", patch_sha256="a" * 64),
                _case(policy="pdb-on-uncertainty", patch_sha256="a" * 64),
            ),
        ).aggregates()["patch_parity"]
        assert identical["tasks"][0]["identical_across_policies"] is True
        assert identical["tasks"][0]["distinct_patch_digests"] == 1
        assert identical["all_comparable_tasks_identical"] is True

        differing = DemoReport(
            environment={},
            cases=(
                _case(policy="static-baseline", patch_sha256="a" * 64),
                _case(policy="pdb-on-uncertainty", patch_sha256="b" * 64),
            ),
        ).aggregates()["patch_parity"]
        assert differing["tasks"][0]["identical_across_policies"] is False
        assert differing["tasks"][0]["distinct_patch_digests"] == 2
        assert differing["all_comparable_tasks_identical"] is False

    def test_patch_parity_is_undecidable_for_a_single_policy_run(self) -> None:
        parity = DemoReport(environment={}, cases=(_case(),)).aggregates()["patch_parity"]
        assert parity["tasks"][0]["identical_across_policies"] is None
        assert parity["comparable_task_count"] == 0
        assert parity["all_comparable_tasks_identical"] is None

    def test_offline_totals_are_summed_from_the_case_ledgers(self) -> None:
        aggregates = DemoReport(
            environment={},
            cases=(
                _case(policy="static-baseline", network_attempts=2),
                _case(policy="pdb-on-uncertainty", provider_attempts=1),
            ),
        ).aggregates()
        assert aggregates["offline"]["network_attempts"] == 2
        assert aggregates["offline"]["provider_attempts"] == 1
        assert "In-process only" in aggregates["offline"]["guard_scope"]

    def test_deterministic_view_strips_only_the_declared_keys(self) -> None:
        mapping = self._report().to_mapping()
        view = deterministic_view(mapping)
        assert set(mapping) - set(view) == set(NONDETERMINISTIC_RESULT_KEYS)
        assert view["cases"] == mapping["cases"]

    def test_results_json_is_stable_and_parses(self) -> None:
        report = self._report()
        first = results_json(report)
        assert first == results_json(report)
        assert json.loads(first)["aggregates"]["case_count"] == 2


class TestSummaryRendering:
    def test_summary_reports_the_measured_figures(self) -> None:
        report = DemoReport(
            environment={
                "git_head": "deadbeef",
                "git_branch": "feature/demo",
                "git_working_tree_dirty": True,
                "source_tree_sha256": "f" * 64,
                "python_version": "3.11.0",
                "platform": "Windows",
                "model_backend": "offline-deterministic-demo",
                "model_provider_policy": "none-configured",
                "network_access_policy": "blocked-in-process",
                "generated_utc": "2026-01-01T00:00:00.000Z",
            },
            cases=(_case(), _case(policy="pdb-on-uncertainty", pdb_observations=3, model_calls=22)),
        )
        text = render_summary(report.to_mapping(), entry_point=DEFAULT_ENTRY_POINT)
        assert "deadbeef" in text
        assert "f" * 64 in text
        assert DEFAULT_ENTRY_POINT in text
        assert "RESOLVED=1" in text
        assert "No case reported a failure" in text
        for claim in UNSUPPORTED_CLAIMS:
            assert claim in text
        for limitation in STANDING_LIMITATIONS:
            assert limitation in text
        assert "**0** refused provider-SDK import(s)" in text
        assert "**0** refused network attempt(s)" in text
        # The stated-policy rows must be visibly separated from measurement.
        assert "| Model-provider policy | none-configured |" in text
        assert "| Network-access policy | blocked-in-process |" in text
        assert "stated policy, not measurements" in text

    def test_summary_reports_a_refused_offline_violation(self) -> None:
        report = DemoReport(
            environment={},
            cases=(_case(network_attempts=3, provider_attempts=2),),
        )
        text = render_summary(report.to_mapping(), entry_point=DEFAULT_ENTRY_POINT)
        assert "**2** refused provider-SDK import(s)" in text
        assert "**3** refused network attempt(s)" in text
        assert "offline guard refused 3 network and 2 provider attempt(s)" in text

    def test_summary_reports_a_controller_verifier_disagreement(self) -> None:
        report = DemoReport(environment={}, cases=(_case(outcome="BREAKING_RESOLVED"),))
        text = render_summary(report.to_mapping(), entry_point=DEFAULT_ENTRY_POINT)
        assert "controller classified RESOLVED but the verifier classified BREAKING_RESOLVED" in text

    def test_summary_records_failures_rejections_and_incomplete_runs(self) -> None:
        report = DemoReport(
            environment={},
            cases=(
                _case(
                    status=DemoCaseStatus.CONTROLLER_STOPPED,
                    final_state="Failed",
                    verifier_status="BASELINE_INVALID",
                    outcome=None,
                    localization=LocalizationOutcome.NO_LOCALIZATION.value,
                    runtime=RuntimeEvidenceOutcome.PDB_NOT_REACHED.value,
                    tool_errors=({"action": "apply_patch", "diagnostic": "denied path"},),
                    diagnostics=("case execution failed: boom",),
                    replay_valid=False,
                ),
            ),
        )
        text = render_summary(report.to_mapping(), entry_point=DEFAULT_ENTRY_POINT)
        assert "case status CONTROLLER_STOPPED" in text
        assert "controller stopped in Failed" in text
        assert "verifier status BASELINE_INVALID" in text
        assert "tool `apply_patch` reported denied path" in text
        assert "case execution failed: boom" in text
        assert "trajectory replay not validated" in text
        assert "No case reported a failure" not in text

    def test_summary_never_asserts_pdb_superiority(self) -> None:
        report = DemoReport(environment={}, cases=(_case(),))
        text = render_summary(report.to_mapping(), entry_point=DEFAULT_ENTRY_POINT)
        assert "does not support" in text
        assert "cannot be read as evidence about debugger usefulness" in text


class TestEnvironmentIdentification:
    def test_environment_identifies_the_tested_tree(self) -> None:
        record = environment_record(REPO_ROOT)
        assert len(record["source_tree_sha256"]) == 64
        # These two are stated policy, not measurement; the measured counters
        # live in each case's "offline" block.
        assert record["model_provider_policy"] == "none-configured"
        assert record["network_access_policy"] == "blocked-in-process"
        assert "In-process only" in record["offline_guard_scope"]
        assert record["python_version"]
        assert record["generated_utc"].endswith("Z")

    def test_environment_digest_tracks_a_real_source_change(self, tmp_path: Path) -> None:
        package = tmp_path / "agentic_debugger"
        package.mkdir()
        (package / "a.py").write_text("x = 1\n", encoding="utf-8")
        before = environment_record(tmp_path)["source_tree_sha256"]
        (package / "a.py").write_text("x = 2\n", encoding="utf-8")
        assert environment_record(tmp_path)["source_tree_sha256"] != before

    def test_fixture_digest_detects_a_curated_fixture_change(self, tmp_path: Path) -> None:
        fixture = tmp_path / "fixture"
        (fixture / "tests").mkdir(parents=True)
        (fixture / "mod.py").write_text("x = 1\n", encoding="utf-8")
        before = fixture_digest(fixture)
        assert fixture_digest(fixture) == before
        (fixture / "mod.py").write_text("x = 2\n", encoding="utf-8")
        assert fixture_digest(fixture) != before

    def test_source_digest_changes_with_the_implementation_bytes(self, tmp_path: Path) -> None:
        package = tmp_path / "agentic_debugger"
        package.mkdir()
        (package / "a.py").write_text("x = 1\n", encoding="utf-8")
        first = source_tree_digest(tmp_path)
        (package / "a.py").write_text("x = 2\n", encoding="utf-8")
        assert source_tree_digest(tmp_path) != first

    def test_source_digest_ignores_bytecode_caches(self, tmp_path: Path) -> None:
        package = tmp_path / "agentic_debugger"
        (package / "__pycache__").mkdir(parents=True)
        (package / "a.py").write_text("x = 1\n", encoding="utf-8")
        first = source_tree_digest(tmp_path)
        (package / "__pycache__" / "a.cpython-311.pyc").write_bytes(b"\x00\x01")
        assert source_tree_digest(tmp_path) == first


class TestRunSelection:
    def test_curated_discovery_matches_the_dataset_directory(self) -> None:
        assert "curated-none-handling-001" in curated_task_ids(REPO_ROOT)

    def test_missing_dataset_root_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(DemoInputError):
            curated_task_ids(tmp_path)

    def test_unknown_task_selection_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(DemoInputError, match="unknown curated task"):
            run_demo(
                repository_root=REPO_ROOT,
                workspace_parent=tmp_path,
                task_ids=["curated-not-real-999"],
            )

    def test_empty_policy_selection_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(DemoInputError, match="at least one"):
            run_demo(
                repository_root=REPO_ROOT,
                workspace_parent=tmp_path,
                task_ids=["curated-none-handling-001"],
                policies=[],
            )


class TestCliContracts:
    def test_parser_requires_an_output_directory(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])
        args = parser.parse_args(["--output-dir", "out"])
        assert args.output_dir == "out"
        assert args.policy is None and args.task_id is None
        assert args.strict is False

    def test_parser_rejects_an_unknown_policy(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--output-dir", "out", "--policy", "always-on"])

    def test_reproduction_document_names_the_tested_state(self) -> None:
        report = DemoReport(
            environment={
                "git_head": "cafebabe",
                "git_branch": "feature/demo",
                "git_working_tree_dirty": False,
                "source_tree_sha256": "a" * 64,
                "python_version": "3.11.0",
                "platform": "Windows",
            },
            cases=(_case(),),
        )
        text = render_reproduction(report, DEFAULT_ENTRY_POINT)
        assert "git checkout cafebabe" in text
        assert DEFAULT_ENTRY_POINT in text
        assert "a" * 64 in text
        assert "No network access" in text

    def test_write_outputs_emits_every_artifact(self, tmp_path: Path) -> None:
        case = _case()
        case = DemoCaseResult(
            **{
                **{
                    field: getattr(case, field)
                    for field in case.__dataclass_fields__
                    if field not in {"semantic_events", "events_jsonl"}
                },
                "semantic_events": ({"event_type": "final"},),
                "events_jsonl": '{"a": 1}\n',
            }
        )
        report = DemoReport(environment={"git_head": "abc"}, cases=(case,))
        written = write_outputs(report, tmp_path, DEFAULT_ENTRY_POINT)
        assert written["results"].is_file()
        assert written["summary"].is_file()
        assert written["reproduce"].is_file()
        assert (tmp_path / "trajectories" / f"{case.case_id}.events.jsonl").is_file()
        assert (tmp_path / "trajectories" / f"{case.case_id}.semantic.json").is_file()
        results = json.loads((tmp_path / RESULTS_FILENAME).read_text(encoding="utf-8"))
        summary = (tmp_path / SUMMARY_FILENAME).read_text(encoding="utf-8")
        assert results["cases"][0]["case_id"] == case.case_id
        assert case.task_id in summary
        assert results["cases"][0]["verifier"]["outcome"] in summary
