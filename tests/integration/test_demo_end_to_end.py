"""End-to-end integration tests for the Task 9 demonstration.

These tests execute the real integrated MVP: the accepted controller drives the
accepted tool registry over a disposable copy of a canonical curated fixture,
real pytest subprocesses run, a real PDB worker pauses at a real breakpoint, and
the accepted Task 7 verifier independently re-evaluates the candidate diff.

The autouse fixture blocks in-process socket use, which proves the
demonstration's own code never reaches the network.  Child processes are
separate interpreters and are not covered by that patch; they run only the
curated pytest suites and the bundled PDB worker, neither of which opens a
socket.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
from pathlib import Path
from typing import Callable

import pytest

from agentic_debugger.demo.cli import (
    RESULTS_FILENAME,
    SUMMARY_FILENAME,
    TRAJECTORY_DIRNAME,
    main,
)
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.demo.runner import (
    CURATED_RELATIVE_ROOT,
    DemoCaseStatus,
    DemoInputError,
    LocalizationOutcome,
    RuntimeEvidenceOutcome,
    run_demo,
    run_demo_case,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TASK_ID = "curated-none-handling-001"
FIXTURE = REPO_ROOT / CURATED_RELATIVE_ROOT / TASK_ID


@pytest.fixture(autouse=True)
def block_in_process_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("Task 9 demonstration attempted network access")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.suffix == ".pyc" or "__pycache__" in path.parts:
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _synthetic_repository(tmp_path: Path, transform: Callable[[Path], None]) -> Path:
    """Build a throwaway repository holding one mutated curated fixture."""

    repo = tmp_path / "synthetic-repo"
    destination = repo / CURATED_RELATIVE_ROOT / TASK_ID
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    transform(destination)
    return repo


def _parent(tmp_path: Path, name: str = "workspaces") -> Path:
    parent = tmp_path / name
    parent.mkdir(parents=True, exist_ok=True)
    return parent


class TestStaticBaselineCase:
    def test_static_case_runs_through_the_real_integrated_paths(
        self, tmp_path: Path
    ) -> None:
        before = _tree_digest(FIXTURE)
        parent = _parent(tmp_path)
        result = run_demo_case(
            repository_root=REPO_ROOT,
            task_id=TASK_ID,
            policy=DemoPolicy.STATIC_BASELINE,
            workspace_parent=parent,
        )

        assert result.status is DemoCaseStatus.COMPLETED
        assert result.diagnostics == ()
        assert result.controller["final_state"] == "Done"
        assert result.controller["stop_reason"] == "done"
        assert result.controller["budget_state"]["pdb_observations"] == 0
        assert result.controller["budget_state"]["test_runs"] == 3

        assert [item["reason"] for item in result.gate_decisions] == ["policy_disabled"]
        assert result.runtime_evidence["outcome"] == RuntimeEvidenceOutcome.PDB_NOT_USED.value
        assert result.runtime_evidence["pdb_action_count"] == 0
        assert not any(
            name.startswith(("start_pdb", "get_stack", "get_frame", "safe_eval"))
            for name in result.controller["tool_calls"]
        )

        assert result.patch["applied"] is True
        assert result.patch["changed_files"] == ["display_name.py"]
        assert (
            result.localization["outcome"]
            == LocalizationOutcome.CORRECT_TARGET_SYMBOL.value
        )
        assert result.controller_validation["outcome"] == "RESOLVED"

        verifier = result.verifier
        assert verifier["executed"] is True
        assert verifier["status"] == "COMPLETED"
        assert verifier["outcome"] == "RESOLVED"
        assert verifier["f2p_passed"] == verifier["f2p_total"] == 1
        assert verifier["p2p_passed"] == verifier["p2p_total"] == 2
        assert verifier["full_suite_status"] == "PASS"
        assert verifier["workspace_lifecycle"] == "CLEANED"
        assert verifier["canonical_fixture_unchanged"] is True

        assert result.trajectory["replay_valid"] is True
        assert result.trajectory["observation_status_counts"]["ok"] == 9
        assert result.trajectory["observation_status_counts"]["rejected"] == 0
        assert result.trajectory["observation_status_counts"]["error"] == 0
        assert result.offline["guard_installed"] is True
        assert result.offline["network_attempts"] == 0
        assert result.offline["provider_attempts"] == 0
        assert result.offline["canonical_fixture_unchanged_by_controller"] is True

        assert _tree_digest(FIXTURE) == before
        assert list(parent.iterdir()) == []

    def test_case_record_is_json_serializable(self, tmp_path: Path) -> None:
        result = run_demo_case(
            repository_root=REPO_ROOT,
            task_id=TASK_ID,
            policy=DemoPolicy.STATIC_BASELINE,
            workspace_parent=_parent(tmp_path),
        )
        json.dumps(result.to_mapping(), allow_nan=False)
        json.dumps(list(result.semantic_events), allow_nan=False)


class TestPdbGatedCase:
    def test_pdb_case_collects_real_bounded_runtime_evidence(self, tmp_path: Path) -> None:
        before = _tree_digest(FIXTURE)
        parent = _parent(tmp_path)
        result = run_demo_case(
            repository_root=REPO_ROOT,
            task_id=TASK_ID,
            policy=DemoPolicy.PDB_ON_UNCERTAINTY,
            workspace_parent=parent,
        )

        assert result.status is DemoCaseStatus.COMPLETED
        assert result.controller["final_state"] == "Done"
        assert [item["allowed"] for item in result.gate_decisions] == [True]
        assert result.gate_decisions[0]["reason"] == "allowed"
        assert result.gate_decisions[0]["policy"] == "on_uncertainty"
        assert result.gate_decisions[0]["failure_reproduced"] is True

        runtime = result.runtime_evidence
        assert runtime["outcome"] == RuntimeEvidenceOutcome.PDB_EVIDENCE_COLLECTED.value
        assert runtime["session_started"] is True
        assert runtime["evidence_collected"] is True
        assert runtime["pdb_observation_names"] == [
            "get_stack_summary",
            "get_frame_locals",
            "safe_eval_expression",
        ]
        assert runtime["diagnosis_change_attributable_to_runtime_evidence"] is False
        assert (
            result.controller["budget_state"]["pdb_observations"]
            == runtime["pdb_observation_attempts"]
            == runtime["pdb_observations_succeeded"]
            == 3
        )

        locals_payloads = [
            event["payload"]["observation"]["payload"]
            for event in result.semantic_events
            if event["event_type"] == "observation" and event["name"] == "get_frame_locals"
        ]
        assert len(locals_payloads) == 1
        entries = {item["name"]: item["value"] for item in locals_payloads[0]["locals"]}
        assert entries["name"]["kind"] == "none"
        assert locals_payloads[0]["state"] == "paused"

        stack_payloads = [
            event["payload"]["observation"]["payload"]
            for event in result.semantic_events
            if event["event_type"] == "observation" and event["name"] == "get_stack_summary"
        ]
        assert stack_payloads[0]["frames"][0]["function"] == "format_display_name"

        assert result.verifier["status"] == "COMPLETED"
        assert result.verifier["outcome"] == "RESOLVED"
        assert result.trajectory["replay_valid"] is True
        assert result.offline["network_attempts"] == 0
        assert result.offline["provider_attempts"] == 0
        assert result.offline["canonical_fixture_unchanged_by_controller"] is True

        assert _tree_digest(FIXTURE) == before
        assert list(parent.iterdir()) == []

    def test_pdb_and_static_cases_share_one_candidate_patch(self, tmp_path: Path) -> None:
        static = run_demo_case(
            repository_root=REPO_ROOT,
            task_id=TASK_ID,
            policy=DemoPolicy.STATIC_BASELINE,
            workspace_parent=_parent(tmp_path, "a"),
        )
        gated = run_demo_case(
            repository_root=REPO_ROOT,
            task_id=TASK_ID,
            policy=DemoPolicy.PDB_ON_UNCERTAINTY,
            workspace_parent=_parent(tmp_path, "b"),
        )
        assert static.patch["sha256"] == gated.patch["sha256"]
        assert gated.controller["model_calls"] > static.controller["model_calls"]
        assert gated.controller["tool_call_count"] > static.controller["tool_call_count"]


class TestDeterminism:
    def test_repeated_cases_are_byte_stable(self, tmp_path: Path) -> None:
        first = run_demo_case(
            repository_root=REPO_ROOT,
            task_id=TASK_ID,
            policy=DemoPolicy.STATIC_BASELINE,
            workspace_parent=_parent(tmp_path, "first"),
        )
        second = run_demo_case(
            repository_root=REPO_ROOT,
            task_id=TASK_ID,
            policy=DemoPolicy.STATIC_BASELINE,
            workspace_parent=_parent(tmp_path, "second"),
        )
        assert first.to_mapping() == second.to_mapping()
        assert list(first.semantic_events) == list(second.semantic_events)
        assert first.events_jsonl != "" and second.events_jsonl != ""

    def test_the_debugger_path_is_byte_stable_too(self, tmp_path: Path) -> None:
        # The PDB path is the one most likely to embed volatile data (pause
        # generations, worker script paths), so it gets its own comparison.
        first = run_demo_case(
            repository_root=REPO_ROOT,
            task_id=TASK_ID,
            policy=DemoPolicy.PDB_ON_UNCERTAINTY,
            workspace_parent=_parent(tmp_path, "pdb-first"),
        )
        second = run_demo_case(
            repository_root=REPO_ROOT,
            task_id=TASK_ID,
            policy=DemoPolicy.PDB_ON_UNCERTAINTY,
            workspace_parent=_parent(tmp_path, "pdb-second"),
        )
        assert first.to_mapping() == second.to_mapping()
        assert list(first.semantic_events) == list(second.semantic_events)

    def test_a_failing_case_record_carries_no_disposable_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Cleanup diagnostics are part of the deterministic section, so a raw
        # OS error naming a mkdtemp directory would break byte stability.
        import agentic_debugger.demo.runner as runner_module

        def exploding_rmtree(path: object, *args: object, **kwargs: object) -> None:
            raise PermissionError(f"[WinError 32] file in use: {path}")

        monkeypatch.setattr(runner_module.shutil, "rmtree", exploding_rmtree)
        parent = _parent(tmp_path)
        result = run_demo_case(
            repository_root=REPO_ROOT,
            task_id=TASK_ID,
            policy=DemoPolicy.STATIC_BASELINE,
            workspace_parent=parent,
        )
        assert result.status is DemoCaseStatus.HARNESS_ERROR
        assert result.diagnostics
        rendered = json.dumps(result.to_mapping())
        assert str(tmp_path) not in rendered
        assert "case-curated-none-handling-001" not in rendered
        assert "task_workspace_" not in rendered
        assert any("<WORKSPACE>" in item for item in result.diagnostics)
        # Restore the real rmtree before removing the deliberately leaked tree.
        monkeypatch.undo()
        shutil.rmtree(parent, ignore_errors=True)


class TestHonestFailureRecording:
    def test_baseline_that_does_not_reproduce_stops_and_is_recorded(
        self, tmp_path: Path
    ) -> None:
        def prefix_the_fix(fixture: Path) -> None:
            path = fixture / "display_name.py"
            path.write_text(
                "def format_display_name(name: str | None) -> str:\n"
                "    if name is None:\n"
                "        name = \"\"\n"
                "    normalized_name = name.strip()\n"
                "    if not normalized_name:\n"
                "        return \"Anonymous\"\n"
                "    return normalized_name.title()\n",
                encoding="utf-8",
                newline="\n",
            )

        repo = _synthetic_repository(tmp_path, prefix_the_fix)
        parent = _parent(tmp_path)
        result = run_demo_case(
            repository_root=repo,
            task_id=TASK_ID,
            policy=DemoPolicy.STATIC_BASELINE,
            workspace_parent=parent,
        )

        assert result.status is DemoCaseStatus.CONTROLLER_STOPPED
        assert result.controller["final_state"] == "Failed"
        assert result.controller_validation["baseline_failure_reproduced"] is False
        assert result.controller_validation["abort_reason"] == "failure_not_reproduced"
        assert result.patch["attempted"] is False
        assert result.patch["applied"] is False
        assert result.localization["outcome"] == LocalizationOutcome.NO_LOCALIZATION.value
        assert result.runtime_evidence["outcome"] == RuntimeEvidenceOutcome.PDB_NOT_REACHED.value
        assert result.gate_decisions == ()
        assert result.verifier["executed"] is True
        assert result.verifier["status"] == "BASELINE_INVALID"
        assert result.verifier["outcome"] is None
        assert result.trajectory["replay_valid"] is True
        assert list(parent.iterdir()) == []

    def test_drifted_reference_repair_is_recorded_as_a_harness_error(
        self, tmp_path: Path
    ) -> None:
        def drift(fixture: Path) -> None:
            path = fixture / "display_name.py"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "normalized_name = name.strip()",
                    'normalized_name = (name or "").strip()',
                ),
                encoding="utf-8",
                newline="\n",
            )

        repo = _synthetic_repository(tmp_path, drift)
        parent = _parent(tmp_path)
        result = run_demo_case(
            repository_root=repo,
            task_id=TASK_ID,
            policy=DemoPolicy.STATIC_BASELINE,
            workspace_parent=parent,
        )

        assert result.status is DemoCaseStatus.HARNESS_ERROR
        assert any("matched 0 times" in item for item in result.diagnostics)
        assert result.verifier["executed"] is False
        assert "verifier skipped" in result.verifier["note"]
        assert result.controller["final_state"] is None
        assert result.patch["applied"] is False
        assert list(parent.iterdir()) == []

    def test_a_regressing_repair_is_caught_by_the_independent_verifier(
        self, tmp_path: Path
    ) -> None:
        """The reference repair fixes F2P but breaks a designated P2P test.

        This is the case the demonstration exists to catch: the controller's own
        coarse regression check and the verifier's per-node evidence must both
        report a non-RESOLVED outcome rather than a green run.
        """

        def make_the_repair_regress(fixture: Path) -> None:
            (fixture / "tests" / "test_display_name.py").write_text(
                "import pytest\n"
                "\n"
                "from display_name import format_display_name\n"
                "\n"
                "\n"
                "def test_missing_display_name_returns_fallback() -> None:\n"
                '    assert format_display_name(None) == "Anonymous"\n'
                "\n"
                "\n"
                "def test_regular_display_name_is_formatted() -> None:\n"
                '    assert format_display_name("Ada Lovelace") == "Ada Lovelace"\n'
                "\n"
                "\n"
                "def test_whitespace_is_normalized() -> None:\n"
                "    with pytest.raises(AttributeError):\n"
                "        format_display_name(None)\n",
                encoding="utf-8",
                newline="\n",
            )

        repo = _synthetic_repository(tmp_path, make_the_repair_regress)
        parent = _parent(tmp_path)
        result = run_demo_case(
            repository_root=repo,
            task_id=TASK_ID,
            policy=DemoPolicy.STATIC_BASELINE,
            workspace_parent=parent,
        )

        assert result.status is DemoCaseStatus.CONTROLLER_STOPPED
        assert result.controller["final_state"] == "Failed"
        assert result.controller_validation["designated_regression_passed"] is False
        assert result.controller_validation["outcome"] == "BREAKING_RESOLVED"
        assert result.controller_validation["abort_reason"] == (
            "controller_outcome_BREAKING_RESOLVED"
        )
        # The verifier ran to completion and independently agrees.
        assert result.verifier["executed"] is True
        assert result.verifier["status"] == "COMPLETED"
        assert result.verifier["outcome"] == "BREAKING_RESOLVED"
        assert result.verifier["f2p_passed"] == 1
        assert result.verifier["p2p_passed"] == 1
        assert result.verifier["p2p_total"] == 2
        assert result.patch["applied"] is True
        assert list(parent.iterdir()) == []

    def test_pdb_probe_failure_is_reported_and_leaves_nothing_behind(
        self, tmp_path: Path
    ) -> None:
        def break_the_probe_anchor(fixture: Path) -> None:
            path = fixture / "display_name.py"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "def format_display_name", "def renamed_display_name"
                ),
                encoding="utf-8",
                newline="\n",
            )

        repo = _synthetic_repository(tmp_path, break_the_probe_anchor)
        parent = _parent(tmp_path)
        result = run_demo_case(
            repository_root=repo,
            task_id=TASK_ID,
            policy=DemoPolicy.PDB_ON_UNCERTAINTY,
            workspace_parent=parent,
        )

        assert result.status is DemoCaseStatus.HARNESS_ERROR
        assert any("format_display_name" in item and "not found" in item
                   for item in result.diagnostics)
        assert result.runtime_evidence["session_started"] is False
        assert result.runtime_evidence["outcome"] == RuntimeEvidenceOutcome.PDB_NOT_REACHED.value
        assert result.verifier["executed"] is False
        assert list(parent.iterdir()) == []


class TestMultiCaseRun:
    def test_run_demo_aggregates_real_cases_across_tasks(self, tmp_path: Path) -> None:
        report = run_demo(
            repository_root=REPO_ROOT,
            workspace_parent=_parent(tmp_path),
            task_ids=[TASK_ID, "curated-off-by-one-002"],
            policies=[DemoPolicy.STATIC_BASELINE],
        )
        mapping = report.to_mapping()
        assert mapping["aggregates"]["case_count"] == 2
        assert mapping["aggregates"]["task_count"] == 2
        buckets = mapping["aggregates"]["by_policy"]
        assert len(buckets) == 1
        assert buckets[0]["policy"] == DemoPolicy.STATIC_BASELINE.value
        assert buckets[0]["cases"] == 2
        assert buckets[0]["verifier_completed"] == 2
        assert buckets[0]["verifier_outcomes"] == {"RESOLVED": 2}
        # One policy means parity is undecidable and must be reported as such.
        parity = mapping["aggregates"]["patch_parity"]
        assert parity["comparable_task_count"] == 0
        assert parity["all_comparable_tasks_identical"] is None
        assert mapping["aggregates"]["offline"]["network_attempts"] == 0
        assert list(_parent(tmp_path).iterdir()) == []

    def test_a_malformed_curated_manifest_is_an_input_error(self, tmp_path: Path) -> None:
        def corrupt(fixture: Path) -> None:
            (fixture / "task.json").write_text("{ not json", encoding="utf-8")

        repo = _synthetic_repository(tmp_path, corrupt)
        with pytest.raises(DemoInputError, match="is invalid"):
            run_demo_case(
                repository_root=repo,
                task_id=TASK_ID,
                policy=DemoPolicy.STATIC_BASELINE,
                workspace_parent=_parent(tmp_path),
            )
        assert list(_parent(tmp_path).iterdir()) == []


class TestInputValidation:
    def test_unknown_task_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(DemoInputError, match="manifest is missing"):
            run_demo_case(
                repository_root=REPO_ROOT,
                task_id="curated-not-real-999",
                policy=DemoPolicy.STATIC_BASELINE,
                workspace_parent=_parent(tmp_path),
            )

    def test_policy_must_be_a_demo_policy(self, tmp_path: Path) -> None:
        with pytest.raises(DemoInputError, match="policy must be"):
            run_demo_case(
                repository_root=REPO_ROOT,
                task_id=TASK_ID,
                policy="static-baseline",  # type: ignore[arg-type]
                workspace_parent=_parent(tmp_path),
            )

    def test_workspace_parent_must_exist(self, tmp_path: Path) -> None:
        with pytest.raises(DemoInputError, match="existing directory"):
            run_demo_case(
                repository_root=REPO_ROOT,
                task_id=TASK_ID,
                policy=DemoPolicy.STATIC_BASELINE,
                workspace_parent=tmp_path / "absent",
            )


class TestCommandLineDemonstration:
    def test_cli_writes_agreeing_machine_and_human_artifacts(self, tmp_path: Path) -> None:
        output = tmp_path / "demo-out"
        exit_code = main(
            [
                "--output-dir",
                str(output),
                "--repository-root",
                str(REPO_ROOT),
                "--task-id",
                TASK_ID,
                "--policy",
                DemoPolicy.STATIC_BASELINE.value,
                "--workspace-parent",
                str(_parent(tmp_path)),
                "--strict",
            ]
        )
        assert exit_code == 0

        results = json.loads((output / RESULTS_FILENAME).read_text(encoding="utf-8"))
        summary = (output / SUMMARY_FILENAME).read_text(encoding="utf-8")
        case = results["cases"][0]

        assert results["environment"]["model_provider_policy"] == "none-configured"
        assert results["environment"]["network_access_policy"] == "blocked-in-process"
        assert results["environment"]["source_tree_sha256"] in summary
        assert case["verifier"]["outcome"] == "RESOLVED"
        assert case["verifier"]["outcome"] in summary
        assert case["localization"]["outcome"] in summary
        assert str(case["controller"]["model_calls"]) in summary
        assert results["aggregates"]["case_count"] == 1
        assert results["aggregates"]["offline"]["network_attempts"] == 0
        assert results["aggregates"]["offline"]["provider_attempts"] == 0
        assert case["offline"]["canonical_fixture_unchanged_by_controller"] is True

        trajectories = output / TRAJECTORY_DIRNAME
        assert (trajectories / f"{case['case_id']}.events.jsonl").is_file()
        semantic = json.loads(
            (trajectories / f"{case['case_id']}.semantic.json").read_text(encoding="utf-8")
        )
        assert semantic[-1]["event_type"] == "final"
        assert semantic[-1]["payload"]["final_state"] == "Done"
        assert (output / "REPRODUCE.md").is_file()

    def test_strict_mode_fails_on_an_incomplete_case(self, tmp_path: Path) -> None:
        def prefix_the_fix(fixture: Path) -> None:
            (fixture / "display_name.py").write_text(
                "def format_display_name(name: str | None) -> str:\n"
                "    if name is None:\n"
                "        name = \"\"\n"
                "    normalized_name = name.strip()\n"
                "    if not normalized_name:\n"
                "        return \"Anonymous\"\n"
                "    return normalized_name.title()\n",
                encoding="utf-8",
                newline="\n",
            )

        repo = _synthetic_repository(tmp_path, prefix_the_fix)
        argv = [
            "--output-dir",
            str(tmp_path / "out"),
            "--repository-root",
            str(repo),
            "--policy",
            DemoPolicy.STATIC_BASELINE.value,
            "--workspace-parent",
            str(_parent(tmp_path, "ws-lenient")),
        ]
        assert main(argv) == 0

        strict_argv = list(argv)
        strict_argv[1] = str(tmp_path / "out-strict")
        strict_argv[-1] = str(_parent(tmp_path, "ws-strict"))
        assert main(strict_argv + ["--strict"]) == 1

        results = json.loads(
            (tmp_path / "out-strict" / RESULTS_FILENAME).read_text(encoding="utf-8")
        )
        assert results["cases"][0]["verifier"]["status"] == "BASELINE_INVALID"

    def test_harness_error_fails_even_without_strict_mode(self, tmp_path: Path) -> None:
        def drift(fixture: Path) -> None:
            path = fixture / "display_name.py"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "normalized_name = name.strip()",
                    'normalized_name = (name or "").strip()',
                ),
                encoding="utf-8",
                newline="\n",
            )

        repo = _synthetic_repository(tmp_path, drift)
        exit_code = main(
            [
                "--output-dir",
                str(tmp_path / "out"),
                "--repository-root",
                str(repo),
                "--policy",
                DemoPolicy.STATIC_BASELINE.value,
                "--workspace-parent",
                str(_parent(tmp_path)),
            ]
        )
        assert exit_code == 1
        results = json.loads((tmp_path / "out" / RESULTS_FILENAME).read_text(encoding="utf-8"))
        assert results["cases"][0]["status"] == "HARNESS_ERROR"
        summary = (tmp_path / "out" / SUMMARY_FILENAME).read_text(encoding="utf-8")
        assert "case status HARNESS_ERROR" in summary

    def test_unknown_repository_root_exits_with_input_error(self, tmp_path: Path) -> None:
        assert (
            main(
                [
                    "--output-dir",
                    str(tmp_path / "out"),
                    "--repository-root",
                    str(tmp_path / "not-a-repo"),
                ]
            )
            == 2
        )

    def test_cli_lists_the_complete_curated_set(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--output-dir", "unused", "--repository-root", str(REPO_ROOT), "--list-tasks"]) == 0
        listed = capsys.readouterr().out.split()
        assert listed == sorted(listed)
        assert TASK_ID in listed
        assert len(listed) == 5
