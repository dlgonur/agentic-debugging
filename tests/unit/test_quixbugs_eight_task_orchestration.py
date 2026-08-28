"""Tests for the QuixBugs eight-task baseline orchestration script.

Covers the narrow repair scope: verdict integrity (the complete verdict is
only valid for the exact eight declared algorithms, all executed once and all
passed), fail-closed setup (safety-critical checks raise ``SetupError`` and
stop before task execution even under ``python -O``), and candidate-cap
enforcement in the orchestration path.

These tests are offline/fake only -- no WSL, no network, no real sandbox.
The pure selection/verdict/cap functions are imported directly from the
operator script; the fail-closed setup checks are exercised through a fake
``WslProcess``/``WslBubblewrapRunner`` so the ``SetupError`` paths are
reached without invoking the real Bubblewrap self-test or resource probes.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "quixbugs_eight_task_baseline.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("quixbugs_eight_task_baseline", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["quixbugs_eight_task_baseline"] = module
    spec.loader.exec_module(module)
    return module


M = _load_script_module()
M.EXTERNAL_ROOT_POSIX = "/tmp/agentic-debugger-quixbugs-test"

DECLARED = list(M.EXPECTED_SELECTED_ALGORITHMS)
EIGHT = list(M.EXPECTED_SELECTED_ALGORITHMS)


# ---- Verdict integrity (list-based, exactly-once semantics) ----------------


def test_complete_verdict_requires_exact_declared_list_solved_and_executed_once() -> None:
    assert M.complete_verdict(EIGHT, EIGHT, DECLARED) == "ACCEPT_CANDIDATE_EIGHT_TASK_BASELINE_COMPLETE"


@pytest.mark.parametrize(
    "solved,executed,label",
    [
        ([], [], "empty run"),
        (["gcd"], ["gcd"], "one-task subset"),
        (EIGHT[:-1], EIGHT[:-1], "seven-task subset"),
        (EIGHT[:-1], EIGHT, "eight executed but only seven solved"),
        (EIGHT, EIGHT[:-1], "solved list shorter than executed"),
        (EIGHT, [], "solved without execution"),
    ],
)
def test_complete_verdict_rejects_empty_subset_and_partial_runs(solved: list, executed: list, label: str) -> None:
    assert M.complete_verdict(solved, executed, DECLARED) == "IMPLEMENTED_BASELINE_PARTIALLY_BLOCKED", label


def test_complete_verdict_rejects_extra_algorithm_not_in_declared_list() -> None:
    # 8 executed/8 solved but the list contains bitcount (not declared) instead
    # of kheapsort -- counts match but the list is wrong.
    wrong_eight = [a for a in EIGHT if a != "kheapsort"] + ["bitcount"]
    assert M.complete_verdict(wrong_eight, wrong_eight, DECLARED) == "IMPLEMENTED_BASELINE_PARTIALLY_BLOCKED"


def test_complete_verdict_rejects_superset_with_extra_algorithm() -> None:
    superset = EIGHT + ["bitcount"]
    assert M.complete_verdict(superset, superset, DECLARED) == "IMPLEMENTED_BASELINE_PARTIALLY_BLOCKED"


def test_complete_verdict_rejects_manifest_drifted_list() -> None:
    drifted = [a for a in EIGHT if a != "kheapsort"] + ["mergesort"]
    assert M.complete_verdict(drifted, drifted, DECLARED) == "IMPLEMENTED_BASELINE_PARTIALLY_BLOCKED"


def test_complete_verdict_rejects_duplicate_executed_algorithm() -> None:
    # The defect: reducing to frozenset collapses duplicates. With list-based
    # exactly-once semantics, executing gcd twice (with kheapsort missing)
    # must be rejected even though the frozenset would match the declared set.
    dup_executed = [a for a in EIGHT if a != "kheapsort"] + ["gcd"]
    assert M.complete_verdict(EIGHT, dup_executed, DECLARED) == "IMPLEMENTED_BASELINE_PARTIALLY_BLOCKED"


def test_complete_verdict_rejects_duplicate_solved_algorithm() -> None:
    dup_solved = [a for a in EIGHT if a != "kheapsort"] + ["gcd"]
    assert M.complete_verdict(dup_solved, EIGHT, DECLARED) == "IMPLEMENTED_BASELINE_PARTIALLY_BLOCKED"


def test_complete_verdict_rejects_all_eight_plus_a_duplicate() -> None:
    # All eight declared algorithms present AND a duplicate of one -- 9
    # executions, 9 solved. A frozenset would collapse to 8 and pass; the
    # list must reject this because it is not exactly-once.
    nine = EIGHT + ["gcd"]
    assert M.complete_verdict(nine, nine, DECLARED) == "IMPLEMENTED_BASELINE_PARTIALLY_BLOCKED"


def test_exit_code_zero_only_for_complete_eight_task_baseline() -> None:
    assert M.exit_code_for("ACCEPT_CANDIDATE_EIGHT_TASK_BASELINE_COMPLETE", EIGHT, DECLARED) == 0


@pytest.mark.parametrize(
    "verdict,executed",
    [
        ("IMPLEMENTED_BASELINE_PARTIALLY_BLOCKED", []),
        ("IMPLEMENTED_BASELINE_PARTIALLY_BLOCKED", ["gcd"]),
        ("IMPLEMENTED_BASELINE_PARTIALLY_BLOCKED", EIGHT[:-1]),
        ("IMPLEMENTED_BASELINE_PARTIALLY_BLOCKED", EIGHT),
        ("ACCEPT_CANDIDATE_EIGHT_TASK_BASELINE_COMPLETE", []),
        ("ACCEPT_CANDIDATE_EIGHT_TASK_BASELINE_COMPLETE", EIGHT[:-1]),
        ("ACCEPT_CANDIDATE_EIGHT_TASK_BASELINE_COMPLETE", EIGHT + ["bitcount"]),
        ("ACCEPT_CANDIDATE_EIGHT_TASK_BASELINE_COMPLETE", [a for a in EIGHT if a != "kheapsort"] + ["gcd"]),
    ],
)
def test_exit_code_one_for_any_non_complete_run(verdict: str, executed: list) -> None:
    assert M.exit_code_for(verdict, executed, DECLARED) == 1


def test_verdict_discrepancy_reports_missing_extra_unsolved_and_duplicates() -> None:
    solved = [a for a in EIGHT if a != "kheapsort"]
    executed = EIGHT + ["bitcount", "gcd"]  # extra + duplicate
    disc = M.verdict_discrepancy(solved, executed, DECLARED)
    assert disc["missing_from_declared"] == []
    assert disc["extra_beyond_declared"] == ["bitcount"]
    # gcd is solved (it's in the solved list), so the duplicate gcd execution
    # is not "unsolved" -- only bitcount and kheapsort are unsolved.
    assert disc["executed_not_solved"] == ["bitcount", "kheapsort"]
    assert disc["duplicate_executions"] == ["gcd"]


def test_verdict_discrepancy_reports_missing_for_subset_run() -> None:
    disc = M.verdict_discrepancy([], [], DECLARED)
    assert sorted(disc["missing_from_declared"]) == sorted(DECLARED)
    assert disc["extra_beyond_declared"] == []
    assert disc["executed_not_solved"] == []
    assert disc["duplicate_executions"] == []


def test_load_declared_selected_algorithms_reads_pilot_manifest() -> None:
    declared = M.load_declared_selected_algorithms(M.PILOT_MANIFEST)
    assert list(declared) == DECLARED
    assert len(declared) == 8


def test_load_declared_selected_algorithms_rejects_empty_selected_tasks(tmp_path: Path) -> None:
    import json

    bad = tmp_path / "bad_pilot.json"
    bad.write_text(json.dumps({"selected_tasks": []}), encoding="utf-8")
    with pytest.raises(M.OrchestrationError, match="no selected_tasks"):
        M.load_declared_selected_algorithms(bad)


def test_load_declared_selected_algorithms_rejects_malformed_entry(tmp_path: Path) -> None:
    import json

    bad = tmp_path / "bad_pilot.json"
    bad.write_text(json.dumps({"selected_tasks": [{"algorithm": "gcd"}, {"no_algorithm": True}]}), encoding="utf-8")
    with pytest.raises(M.OrchestrationError, match="malformed"):
        M.load_declared_selected_algorithms(bad)


def test_load_declared_selected_algorithms_rejects_duplicate_entry(tmp_path: Path) -> None:
    import json

    eight = [{"algorithm": a} for a in EIGHT]
    eight.append({"algorithm": "gcd"})  # duplicate
    bad = tmp_path / "bad_pilot.json"
    bad.write_text(json.dumps({"selected_tasks": eight}), encoding="utf-8")
    with pytest.raises(M.OrchestrationError, match="duplicate selected_tasks"):
        M.load_declared_selected_algorithms(bad)


def test_load_declared_selected_algorithms_rejects_wrong_count(tmp_path: Path) -> None:
    import json

    seven = [{"algorithm": a} for a in EIGHT[:7]]
    bad = tmp_path / "bad_pilot.json"
    bad.write_text(json.dumps({"selected_tasks": seven}), encoding="utf-8")
    with pytest.raises(M.OrchestrationError, match="expected exactly 8"):
        M.load_declared_selected_algorithms(bad)


# ---- --only selection: exact names, no prefix matching ---------------------


def test_select_manifests_default_returns_all_eight_in_order() -> None:
    selected = M.select_manifests([])
    assert len(selected) == 8
    assert selected == list(M.SELECTED_MANIFESTS)


def test_select_manifests_kth_does_not_match_kheapsort_prefix() -> None:
    # The defect: prefix matching made --only kth select both kth and kheapsort.
    # Exact matching must select only kth.
    selected = M.select_manifests(["kth"])
    assert len(selected) == 1
    assert selected[0].endswith("KTH_SMOKE_MANIFEST_V1.json")


def test_select_manifests_kheapsort_does_not_match_kth_prefix() -> None:
    selected = M.select_manifests(["kheapsort"])
    assert len(selected) == 1
    assert selected[0].endswith("KHEAPSORT_SMOKE_MANIFEST_V1.json")


def test_select_manifests_both_kth_and_kheapsort_selected_explicitly() -> None:
    selected = M.select_manifests(["kth", "kheapsort"])
    assert len(selected) == 2
    names = {Path(m).name for m in selected}
    assert names == {"KTH_SMOKE_MANIFEST_V1.json", "KHEAPSORT_SMOKE_MANIFEST_V1.json"}


def test_select_manifests_one_task_subset_is_not_complete() -> None:
    selected = M.select_manifests(["gcd"])
    assert len(selected) == 1
    # A one-task subset that passes must still not be the complete verdict:
    # the executed list (['gcd']) is not the declared eight-algorithm list.
    one = ["gcd"]
    assert M.complete_verdict(one, one, DECLARED) == "IMPLEMENTED_BASELINE_PARTIALLY_BLOCKED"
    assert M.exit_code_for("IMPLEMENTED_BASELINE_PARTIALLY_BLOCKED", one, DECLARED) == 1


def test_select_manifests_unknown_name_raises_not_silently_empty() -> None:
    # The defect: an unknown --only value produced an empty selected list,
    # and an empty run returned the complete verdict with exit 0.
    with pytest.raises(M.OrchestrationError, match="unknown --only"):
        M.select_manifests(["nonexistent_algorithm"])


def test_select_manifests_duplicate_name_raises() -> None:
    with pytest.raises(M.OrchestrationError, match="duplicate"):
        M.select_manifests(["gcd", "gcd"])


def test_select_manifests_case_variant_duplicate_raises() -> None:
    # gcd and GCD resolve to the same algorithm; this is a duplicate, not two tasks.
    with pytest.raises(M.OrchestrationError, match="duplicate"):
        M.select_manifests(["gcd", "GCD"])


def test_select_manifests_mixed_unknown_and_known_raises_on_unknown() -> None:
    with pytest.raises(M.OrchestrationError, match="unknown --only"):
        M.select_manifests(["gcd", "typo_algorithm"])


def test_select_manifests_is_case_insensitive() -> None:
    selected = M.select_manifests(["GCD"])
    assert len(selected) == 1
    assert selected[0].endswith("GCD_SMOKE_MANIFEST_V1.json")


# ---- Candidate cap enforcement --------------------------------------------


def test_enforce_candidate_cap_allows_within_cap() -> None:
    # 8 selected + 3 excluded = 11, within the 12 cap.
    M.enforce_candidate_cap(8, 3)


def test_enforce_candidate_cap_rejects_over_cap() -> None:
    with pytest.raises(M.OrchestrationError, match="candidate cap exceeded"):
        M.enforce_candidate_cap(8, 5)  # 13 > 12


def test_enforce_candidate_cap_boundary_at_exactly_twelve() -> None:
    # 12 exactly is within the cap (not exceeded).
    M.enforce_candidate_cap(8, 4)


# ---- Fail-closed setup: SetupError, not assert (survives python -O) --------


class _FakeCommandResult:
    def __init__(self, exit_code: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _FakeWslProcess:
    """Offline stand-in for WslProcess -- never invokes WSL."""

    def __init__(self, results: dict[str, _FakeCommandResult] | None = None) -> None:
        self._results = results or {}
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], timeout_seconds: int) -> _FakeCommandResult:
        self.calls.append(list(argv))
        script = " ".join(argv)
        for key, result in self._results.items():
            if key in script:
                return result
        return _FakeCommandResult(exit_code=0, stdout="done")


class _FakeRunner:
    """Offline stand-in for WslBubblewrapRunner with controllable self-test /
    resource-readiness outcomes."""

    def __init__(
        self,
        *,
        selftest_results: dict[str, Any] | None = None,
        resource_isolation_ready: bool = True,
        boundary_guarantee: dict | None = None,
    ) -> None:
        self.resource_isolation_ready = resource_isolation_ready
        self.boundary_guarantee = boundary_guarantee or {"resource_limits": {}}
        self._selftest_results = selftest_results

    def self_test(self, workspace_host: str, expected_python_version: str = "3.10.12") -> dict[str, Any]:
        if self._selftest_results is None:
            return {
                "network_denied": {"passed": True, "exit_code": 1, "stderr": ""},
                "windows_mounts_hidden": {"passed": True, "exit_code": 0, "stderr": ""},
                "unrelated_home_hidden": {"passed": True, "exit_code": 0, "stderr": ""},
                "owned_workspace_write": {"passed": True, "exit_code": 0, "stderr": ""},
                "runtime_mount_read_only": {"passed": True, "exit_code": 1, "stderr": ""},
                "child_process_isolated": {"passed": True, "exit_code": 0, "stderr": ""},
                "exact_interpreter": {"passed": True, "exit_code": 0, "stderr": ""},
            }
        return self._selftest_results

    def verify_and_open_resource_isolation(self, workspace_host: str, profile: Any) -> dict[str, Any]:
        return {"cpu_limit_enforced": {"passed": self.resource_isolation_ready}}


def test_setup_error_is_runtimeerror_not_assertion(monkeypatch) -> None:
    # A bare assert would be stripped by python -O; SetupError is a real
    # exception that survives optimization.
    assert issubclass(M.SetupError, RuntimeError)
    # Confirm the script no longer uses bare assert for safety-critical setup
    # checks (the defect). We grep the source for the old assert patterns.
    source = SCRIPT.read_text(encoding="utf-8")
    # The old defect used these exact assert statements in _setup_environment.
    assert 'assert result.exit_code == 0, "failed to create external root layout"' not in source
    assert 'assert result.exit_code == 0, "failed to build task-local venv"' not in source
    assert "assert all(entry[\"passed\"] for entry in bwrap_results.values())" not in source
    assert "assert runner.resource_isolation_ready is True" not in source


def test_setup_requires_an_explicit_external_root(monkeypatch) -> None:
    monkeypatch.setattr(M, "EXTERNAL_ROOT_POSIX", "")
    with pytest.raises(M.SetupError, match="AGENTIC_DEBUGGER_QUIXBUGS_ROOT"):
        M._setup_environment()


def test_setup_raises_on_layout_failure(monkeypatch) -> None:
    fake_process = _FakeWslProcess({"mkdir -p": _FakeCommandResult(exit_code=1, stderr="permission denied")})

    raised = False
    try:
        with monkeypatch.context() as mc:
            mc.setattr(M, "WslProcess", lambda distro: fake_process)
            mc.setattr(M, "fingerprint_environment", lambda env: "fake")
            mc.setattr(M, "wsl_unc_path", lambda posix, distro: f"\\\\fake\\{posix}")
            mc.setattr(M, "WslBubblewrapRunner", lambda **kw: _FakeRunner())
            M._setup_environment()
    except M.SetupError as exc:
        raised = True
        assert "layout" in str(exc).lower()
    assert raised, "SetupError must be raised on layout failure, not a stripped assert"


def test_setup_raises_on_bubblewrap_selftest_failure(monkeypatch) -> None:
    fake_process = _FakeWslProcess()
    failed_selftest = {
        "network_denied": {"passed": False, "exit_code": 0, "stderr": "network was reachable"},
        "windows_mounts_hidden": {"passed": True, "exit_code": 0, "stderr": ""},
        "unrelated_home_hidden": {"passed": True, "exit_code": 0, "stderr": ""},
        "owned_workspace_write": {"passed": True, "exit_code": 0, "stderr": ""},
        "runtime_mount_read_only": {"passed": True, "exit_code": 1, "stderr": ""},
        "child_process_isolated": {"passed": True, "exit_code": 0, "stderr": ""},
        "exact_interpreter": {"passed": True, "exit_code": 0, "stderr": ""},
    }

    raised = False
    try:
        with monkeypatch.context() as mc:
            mc.setattr(M, "WslProcess", lambda distro: fake_process)
            mc.setattr(M, "fingerprint_environment", lambda env: "fake")
            mc.setattr(M, "wsl_unc_path", lambda posix, distro: f"\\\\fake\\{posix}")
            mc.setattr(M, "WslBubblewrapRunner", lambda **kw: _FakeRunner(selftest_results=failed_selftest))
            M._setup_environment()
    except M.SetupError as exc:
        raised = True
        assert "self-test" in str(exc).lower()
    assert raised, "SetupError must be raised on Bubblewrap self-test failure"


def test_setup_raises_when_resource_isolation_gate_stays_closed(monkeypatch) -> None:
    fake_process = _FakeWslProcess()

    raised = False
    try:
        with monkeypatch.context() as mc:
            mc.setattr(M, "WslProcess", lambda distro: fake_process)
            mc.setattr(M, "fingerprint_environment", lambda env: "fake")
            mc.setattr(M, "wsl_unc_path", lambda posix, distro: f"\\\\fake\\{posix}")
            mc.setattr(M, "WslBubblewrapRunner", lambda **kw: _FakeRunner(resource_isolation_ready=False))
            M._setup_environment()
    except M.SetupError as exc:
        raised = True
        assert "resource isolation" in str(exc).lower()
    assert raised, "SetupError must be raised when the resource-isolation gate does not open"


def test_setup_raises_on_venv_bootstrap_failure(monkeypatch) -> None:
    # venv_reusable=False path: the bootstrap script returns non-zero.
    fake_process = _FakeWslProcess({
        "pytest --version": _FakeCommandResult(exit_code=1, stdout="pytest not installed"),
        "rm -rf": _FakeCommandResult(exit_code=1, stderr="venv bootstrap failed"),
    })

    raised = False
    try:
        with monkeypatch.context() as mc:
            mc.setattr(M, "WslProcess", lambda distro: fake_process)
            mc.setattr(M, "fingerprint_environment", lambda env: "fake")
            mc.setattr(M, "wsl_unc_path", lambda posix, distro: f"\\\\fake\\{posix}")
            mc.setattr(M, "WslBubblewrapRunner", lambda **kw: _FakeRunner())
            M._setup_environment()
    except M.SetupError as exc:
        raised = True
        assert "venv" in str(exc).lower()
    assert raised, "SetupError must be raised on venv bootstrap failure"


# ---- Candidate accounting in the pilot manifest ---------------------------


def test_pilot_manifest_candidate_accounting_is_correct() -> None:
    import json

    manifest = json.loads((REPO_ROOT / "research" / "quixbugs" / "EIGHT_TASK_PILOT_MANIFEST_V1.json").read_text(encoding="utf-8"))
    accounting = manifest["candidate_accounting"]
    # 8 selected + 3 excluded = 11 unique algorithms executed through the
    # resource-limited runner (not "through the verifier" -- get_factors did
    # not reach the EvaluationVerifier).
    assert accounting["selected_count"] == 8
    assert accounting["excluded_count"] == 3
    assert accounting["total_through_resource_limited_runner"] == 11
    assert accounting["resource_limited_runner_execution_count"] == 11
    # Completed-verifier count is 8 (only the selected eight reached the
    # EvaluationVerifier); get_factors did NOT reach the verifier.
    assert accounting["completed_verifier_count"] == 8
    # Stages are distinguished accurately.
    assert len(accounting["excluded_by_stage"]["discovery_excluded"]) == 2
    assert len(accounting["excluded_by_stage"]["pre_verifier_schema_excluded"]) == 1
    # The historical cap compliance is unproven (not "11 of 12").
    assert accounting["historical_cap_compliance"] == "unproven"
    assert "11 of 12" not in accounting["historical_cap_compliance_note"]
    assert "cannot be proven" in accounting["historical_cap_compliance_note"].lower() or "unproven" in accounting["historical_cap_compliance_note"].lower()
    # The misrepresentation of unsandboxed pytest as read-only must be corrected:
    # the stage_distinction must describe the triage as test execution outside
    # the resource-limited runner, not read-only metadata inspection.
    stages = accounting["stage_distinction"]
    triage_desc = stages["historical_unsandboxed_triage"].lower()
    assert "test execution" in triage_desc or "pytest" in triage_desc
    assert "not read-only metadata inspection" in stages["historical_unsandboxed_triage"]
    # get_factors must be explicitly noted as not reaching the EvaluationVerifier.
    get_factors_entry = accounting["excluded_by_stage"]["pre_verifier_schema_excluded"][0]
    assert "did NOT reach the EvaluationVerifier" in get_factors_entry["reason"]
    # Future runs must be fail-closed at 12 unique attempted algorithms.
    assert "12" in accounting["future_runs_policy"]
    assert "static file/metadata inspection" in accounting["future_runs_policy"]


def test_pilot_manifest_does_not_assert_eleven_of_twelve_as_fact() -> None:
    import json

    raw = (REPO_ROOT / "research" / "quixbugs" / "EIGHT_TASK_PILOT_MANIFEST_V1.json").read_text(encoding="utf-8")
    # The stale "11 of 12" claim must not be asserted as a fact. The only
    # acceptable mention of "11-of-12" is in the note stating it has been
    # removed; it must not appear as a standalone claim.
    assert "11 of 12" not in raw
    # "11-of-12" may appear only in the explicit removal statement, not as a
    # positive accounting claim.
    import re

    positive_claims = re.findall(r"(?<!removed\. )(?<!has been removed\.)\b11-of-12\b", raw)
    # The only occurrence should be inside the "has been removed" sentence.
    assert raw.count("11-of-12") <= 1, "11-of-12 must appear at most once (in the removal statement)"


def test_pilot_manifest_distinguishes_runner_execution_from_verifier_execution() -> None:
    import json

    manifest = json.loads((REPO_ROOT / "research" / "quixbugs" / "EIGHT_TASK_PILOT_MANIFEST_V1.json").read_text(encoding="utf-8"))
    stages = manifest["candidate_accounting"]["stage_distinction"]
    assert "completed_verifier" in stages
    assert "resource_limited_runner_execution" in stages
    assert "historical_unsandboxed_triage" in stages
    # The runner-execution count (11) must be greater than the
    # completed-verifier count (8) because get_factors ran through the
    # runner but did not reach the verifier.
    assert manifest["candidate_accounting"]["resource_limited_runner_execution_count"] > manifest["candidate_accounting"]["completed_verifier_count"]


# ---- validate_campaign: all local validation before environment side effects


def _make_excluded_manifest(tmp_path: Path, algorithm: str) -> Path:
    import json

    path = tmp_path / f"{algorithm}_excluded.json"
    path.write_text(json.dumps({"target": {"algorithm": algorithm}}), encoding="utf-8")
    return path


def _make_pilot_manifest(tmp_path: Path, algorithms: list[str]) -> Path:
    import json

    path = tmp_path / "pilot.json"
    path.write_text(json.dumps({"selected_tasks": [{"algorithm": a} for a in algorithms]}), encoding="utf-8")
    return path


def test_validate_campaign_succeeds_for_valid_input(tmp_path: Path) -> None:
    pilot = _make_pilot_manifest(tmp_path, EIGHT)
    selected, declared, excluded = M.validate_campaign([], [], skip_excluded=True, pilot_manifest_path=pilot)
    assert list(declared) == EIGHT
    assert selected == list(M.SELECTED_MANIFESTS)
    assert excluded == []


def test_validate_campaign_loads_excluded_identities(tmp_path: Path) -> None:
    pilot = _make_pilot_manifest(tmp_path, EIGHT)
    exc1 = _make_excluded_manifest(tmp_path, "bitcount")
    exc2 = _make_excluded_manifest(tmp_path, "find_first_in_sorted")
    selected, declared, excluded = M.validate_campaign([], [exc1, exc2], pilot_manifest_path=pilot)
    assert excluded == ["bitcount", "find_first_in_sorted"]


def test_validate_campaign_rejects_unknown_only(tmp_path: Path) -> None:
    pilot = _make_pilot_manifest(tmp_path, EIGHT)
    with pytest.raises(M.OrchestrationError, match="unknown --only"):
        M.validate_campaign(["nonexistent"], [], skip_excluded=True, pilot_manifest_path=pilot)


def test_validate_campaign_rejects_duplicate_only(tmp_path: Path) -> None:
    pilot = _make_pilot_manifest(tmp_path, EIGHT)
    with pytest.raises(M.OrchestrationError, match="duplicate"):
        M.validate_campaign(["gcd", "gcd"], [], skip_excluded=True, pilot_manifest_path=pilot)


def test_validate_campaign_rejects_duplicate_pilot_manifest_entry(tmp_path: Path) -> None:
    eight_dup = EIGHT + ["gcd"]
    pilot = _make_pilot_manifest(tmp_path, eight_dup)
    with pytest.raises(M.OrchestrationError, match="duplicate selected_tasks"):
        M.validate_campaign([], [], skip_excluded=True, pilot_manifest_path=pilot)


def test_validate_campaign_rejects_wrong_count_pilot_manifest(tmp_path: Path) -> None:
    pilot = _make_pilot_manifest(tmp_path, EIGHT[:7])
    with pytest.raises(M.OrchestrationError, match="expected exactly 8"):
        M.validate_campaign([], [], skip_excluded=True, pilot_manifest_path=pilot)


def test_validate_campaign_rejects_malformed_pilot_manifest(tmp_path: Path) -> None:
    import json

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"selected_tasks": [{"algorithm": "gcd"}, {"no_algo": True}]}), encoding="utf-8")
    with pytest.raises(M.OrchestrationError, match="malformed"):
        M.validate_campaign([], [], skip_excluded=True, pilot_manifest_path=bad)


def test_validate_campaign_rejects_manifest_drift(monkeypatch, tmp_path: Path) -> None:
    pilot = _make_pilot_manifest(tmp_path, EIGHT)
    # Simulate a drifted SELECTED_MANIFESTS: replace one algorithm.
    drifted = list(EIGHT)
    drifted[0] = "mergesort"
    monkeypatch.setattr(M, "SELECTED_MANIFESTS", [f"research/quixbugs/{a.upper()}_SMOKE_MANIFEST_V1.json" for a in drifted])
    with pytest.raises(M.OrchestrationError, match="manifest drift"):
        M.validate_campaign([], [], skip_excluded=True, pilot_manifest_path=pilot)


def test_validate_campaign_rejects_duplicate_in_selected_manifests_constant(monkeypatch, tmp_path: Path) -> None:
    pilot = _make_pilot_manifest(tmp_path, EIGHT)
    dup_constant = list(M.SELECTED_MANIFESTS)
    dup_constant.append(dup_constant[0])  # duplicate the first entry
    monkeypatch.setattr(M, "SELECTED_MANIFESTS", dup_constant)
    with pytest.raises(M.OrchestrationError, match="duplicate algorithm"):
        M.validate_campaign([], [], skip_excluded=True, pilot_manifest_path=pilot)


def test_validate_campaign_rejects_duplicate_excluded(tmp_path: Path) -> None:
    pilot = _make_pilot_manifest(tmp_path, EIGHT)
    exc = _make_excluded_manifest(tmp_path, "bitcount")
    with pytest.raises(M.OrchestrationError, match="duplicate excluded"):
        M.validate_campaign([], [exc, exc], pilot_manifest_path=pilot)


def test_validate_campaign_rejects_selected_excluded_overlap(tmp_path: Path) -> None:
    pilot = _make_pilot_manifest(tmp_path, EIGHT)
    exc = _make_excluded_manifest(tmp_path, "gcd")  # gcd is also a selected task
    with pytest.raises(M.OrchestrationError, match="overlap"):
        M.validate_campaign([], [exc], pilot_manifest_path=pilot)


def test_validate_campaign_enforces_candidate_cap(tmp_path: Path) -> None:
    pilot = _make_pilot_manifest(tmp_path, EIGHT)
    # 8 selected + 5 excluded = 13 > 12
    excs = [_make_excluded_manifest(tmp_path, f"algo{i}") for i in range(5)]
    with pytest.raises(M.OrchestrationError, match="candidate cap exceeded"):
        M.validate_campaign([], excs, pilot_manifest_path=pilot)


# ---- _setup_environment must not be called for invalid input --------------


class _CallTracker:
    """Wraps _setup_environment to detect if it was called."""

    def __init__(self) -> None:
        self.called = False

    def __call__(self, *args, **kwargs):
        self.called = True
        raise AssertionError("_setup_environment must not be called for invalid input")


def _patch_setup_environment(monkeypatch) -> _CallTracker:
    tracker = _CallTracker()
    monkeypatch.setattr(M, "_setup_environment", tracker)
    return tracker


def test_setup_not_called_for_unknown_only(monkeypatch, tmp_path: Path) -> None:
    tracker = _patch_setup_environment(monkeypatch)
    with pytest.raises(M.OrchestrationError, match="unknown --only"):
        M.validate_campaign(["nonexistent"], [], skip_excluded=True)
    assert not tracker.called


def test_setup_not_called_for_duplicate_selection(monkeypatch, tmp_path: Path) -> None:
    tracker = _patch_setup_environment(monkeypatch)
    with pytest.raises(M.OrchestrationError, match="duplicate"):
        M.validate_campaign(["gcd", "gcd"], [], skip_excluded=True)
    assert not tracker.called


def test_setup_not_called_for_malformed_pilot_manifest(monkeypatch, tmp_path: Path) -> None:
    tracker = _patch_setup_environment(monkeypatch)
    import json

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"selected_tasks": [{"no_algo": True}]}), encoding="utf-8")
    with pytest.raises(M.OrchestrationError, match="malformed"):
        M.validate_campaign([], [], skip_excluded=True, pilot_manifest_path=bad)
    assert not tracker.called


def test_setup_not_called_for_drifted_pilot_manifest(monkeypatch, tmp_path: Path) -> None:
    tracker = _patch_setup_environment(monkeypatch)
    pilot = _make_pilot_manifest(tmp_path, [a if a != "kheapsort" else "mergesort" for a in EIGHT])
    with pytest.raises(M.OrchestrationError, match="manifest drift"):
        M.validate_campaign([], [], skip_excluded=True, pilot_manifest_path=pilot)
    assert not tracker.called


def test_setup_not_called_for_candidate_cap_violation(monkeypatch, tmp_path: Path) -> None:
    tracker = _patch_setup_environment(monkeypatch)
    pilot = _make_pilot_manifest(tmp_path, EIGHT)
    excs = [_make_excluded_manifest(tmp_path, f"algo{i}") for i in range(5)]
    with pytest.raises(M.OrchestrationError, match="candidate cap exceeded"):
        M.validate_campaign([], excs, pilot_manifest_path=pilot)
    assert not tracker.called
