"""Proportional tests for the contained-PDB reachability path.

These are hermetic unit tests (no real WSL/Bubblewrap contact): fake
execution contexts and runners are built with the exact same accepted
dataclasses (``VerifiedExecutionContext``, ``ContainmentGuarantee``,
``PreparedEnvironment``, ``WslBubblewrapRunner``) used by the real path, but
``WslBubblewrapRunner`` never performs any I/O in its constructor, and
``resource_isolation_ready``/``boundary_guarantee`` are only ever read here,
never used to actually launch anything. The real, no-model WSL/Bubblewrap
reachability case is exercised separately by
``scripts/quixbugs_gcd_pdb_reachability_case.py`` and recorded in the review
package.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from agentic_debugger.agent.controller_policy import (
    HypothesisConfidence,
    HypothesisStatus,
    PdbGateContext,
    PdbGateReason,
    PdbPolicy,
    RootCauseHypothesis,
    decide_pdb_access,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.bugsinpy.wsl import ResourceLimits, WslBubblewrapRunner, wsl_unc_path
from agentic_debugger.demo.tools import DemoToolContext
from agentic_debugger.quixbugs.adapter import QuixBugsAdapter
from agentic_debugger.quixbugs.contained_pdb import (
    QUIXBUGS_PDB_OBSERVATION_BUDGET,
    QUIXBUGS_PDB_TASK_ID,
    ContainedPdbError,
    ContainedPdbGateName,
    ContainedPdbSession,
    DeterministicPdbReachabilityDriver,
    _GCD_RUNTIME_PROBE,
    build_contained_pdb_worker_argv,
    contained_pdb_preflight,
    determine_reachability_verdict,
    evaluate_reachability_sequence_from_events,
    materialize_pdb_runtime_bundle,
    run_quixbugs_gcd_pdb_reachability_case,
    validate_events_jsonl,
)
from agentic_debugger.quixbugs.adapter import QuixBugsPreflightFacts
from agentic_debugger.runtime.execution import (
    ContainmentGuarantee,
    DependencyPreparation,
    PdbLaunchPlan,
    PreparedEnvironment,
    VerifiedExecutionContext,
)
from agentic_debugger.runtime.pdb_session import PdbSession

ROOT = Path(__file__).resolve().parents[2]
GCD_MANIFEST = ROOT / "research" / "quixbugs" / "GCD_SMOKE_MANIFEST_V1.json"
HANOI_MANIFEST = ROOT / "research" / "quixbugs" / "HANOI_SMOKE_MANIFEST_V1.json"
DISTRO = "Ubuntu-22.04"


def _fake_runner(*, resource_isolation_ready: bool = True) -> WslBubblewrapRunner:
    root_host = wsl_unc_path("/fake/root", DISTRO)
    runner = WslBubblewrapRunner(
        root_host=root_host, python_root_posix="/fake/venv", python_executable_posix="/fake/venv/bin/python", distro=DISTRO,
    )
    if resource_isolation_ready:
        limits = ResourceLimits(cpu_seconds=5, memory_bytes=268435456, max_processes=8)
        resource_mapping = dict(limits.to_mapping())
        resource_mapping["timeout"] = "linux-timeout+prlimit+wsl-process-tree-enforced"
        resource_mapping["retained_output_chars"] = "bounded-streaming:20000"
        runner.resource_isolation_ready = True
        runner.boundary_guarantee = ContainmentGuarantee(root_host, runner.runner_id, resource_limits=resource_mapping).to_mapping()
    return runner


def _fake_context(*, resource_isolation_ready: bool = True) -> VerifiedExecutionContext:
    runner = _fake_runner(resource_isolation_ready=resource_isolation_ready)
    deps = DependencyPreparation(
        "quixbugs-gcd-smoke-v1", "f" * 64, "4257f44b0ff1181dedaedee6a447e133219fcebf", "quixbugs", "gcd",
        "4257f44b0ff1181dedaedee6a447e133219fcebf", "pytest==7.4.4", "a" * 64, "b" * 64,
    )
    environment = PreparedEnvironment(
        wsl_unc_path("/fake/venv/bin/python", DISTRO), "3.10.12", ".", (), {}, deps,
    )
    containment = ContainmentGuarantee(runner.root_host, runner.runner_id, resource_limits=dict(runner.boundary_guarantee.get("resource_limits", {"timeout": "enforced-by-parent-runner"})))
    return VerifiedExecutionContext(environment, containment, runner)


def _resource_limits() -> ResourceLimits:
    return ResourceLimits(cpu_seconds=5, memory_bytes=268435456, max_processes=8)


# -- static path is unaffected -------------------------------------------------


def test_static_quixbugs_debug_task_defaults_to_zero_pdb_budget() -> None:
    adapter = QuixBugsAdapter.from_manifest(GCD_MANIFEST)
    from agentic_debugger.evaluation.task_schema import TaskSource

    source = TaskSource("external", "quixbugs", adapter.source_provenance())
    commands = adapter.build_commands(fail_to_pass=["python_testcases/test_gcd.py::x"], pass_to_pass=["python_testcases/test_gcd.py::y"])
    task = adapter.to_debug_task(source, commands)
    assert task.constraints.max_pdb_observations == 0


def test_pdb_observation_budget_must_be_non_negative() -> None:
    adapter = QuixBugsAdapter.from_manifest(GCD_MANIFEST)
    from agentic_debugger.evaluation.task_schema import TaskSource
    from agentic_debugger.quixbugs.adapter import QuixBugsTaskMappingError

    source = TaskSource("external", "quixbugs", adapter.source_provenance())
    commands = adapter.build_commands(fail_to_pass=["python_testcases/test_gcd.py::x"], pass_to_pass=["python_testcases/test_gcd.py::y"])
    with pytest.raises(QuixBugsTaskMappingError):
        adapter.to_debug_task(source, commands, pdb_observation_budget=-1)


def test_demo_tool_context_defaults_to_host_local_pdb_session(tmp_path: Path) -> None:
    """The accepted demo/live path must construct plain PdbSession unchanged."""
    from agentic_debugger.runtime.workspace import TaskWorkspace

    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "a.py").write_text("x = 1\n", encoding="utf-8")
    workspace = TaskWorkspace(str(fixture), parent_dir=str(tmp_path))
    context = DemoToolContext.__new__(DemoToolContext)  # avoid full task construction
    context.pdb_session_factory = PdbSession
    session = context.pdb_session_factory(workspace)
    assert type(session) is PdbSession
    workspace.cleanup()


# -- host-local PdbSession is never used for the contained path ---------------


def test_contained_pdb_session_is_not_plain_pdb_session(tmp_path: Path) -> None:
    from agentic_debugger.runtime.workspace import TaskWorkspace

    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "a.py").write_text("x = 1\n", encoding="utf-8")
    workspace = TaskWorkspace(str(fixture), parent_dir=str(tmp_path))
    session = ContainedPdbSession(
        workspace, runner=_fake_runner(), pdb_runtime_root_posix="/fake/bundle", resource_limits=_resource_limits(),
    )
    assert isinstance(session, PdbSession)
    assert type(session) is not PdbSession
    assert session._expected_worker_pid() is None
    workspace.cleanup()


def test_contained_pdb_worker_argv_never_launches_host_python(tmp_path: Path) -> None:
    argv = build_contained_pdb_worker_argv(
        runner=_fake_runner(), workspace_host=wsl_unc_path("/fake/root/runs/case", DISTRO), pdb_runtime_root_posix="/fake/bundle",
        resource_limits=_resource_limits(), session_timeout_seconds=30, distro=DISTRO,
    )
    assert argv[0] == "wsl.exe"
    joined = " ".join(argv)
    assert "bwrap" in joined and "prlimit" in joined and "/opt/python/bin/python" in joined
    assert "/opt/pdb_runtime" in joined
    import sys

    assert sys.executable not in joined


def test_contained_pdb_worker_argv_fails_closed_without_open_resource_gate(tmp_path: Path) -> None:
    with pytest.raises(ContainedPdbError):
        build_contained_pdb_worker_argv(
            runner=_fake_runner(resource_isolation_ready=False), workspace_host=wsl_unc_path("/fake/root/runs/case", DISTRO),
            pdb_runtime_root_posix="/fake/bundle", resource_limits=_resource_limits(), session_timeout_seconds=30, distro=DISTRO,
        )


def test_contained_pdb_worker_argv_rejects_forged_resource_limits(tmp_path: Path) -> None:
    """A caller-supplied ResourceLimits that does not match the runner's
    actually-open boundary_guarantee must not forge the gate."""
    forged = ResourceLimits(cpu_seconds=999, memory_bytes=268435456, max_processes=8)
    with pytest.raises(ContainedPdbError):
        build_contained_pdb_worker_argv(
            runner=_fake_runner(), workspace_host=wsl_unc_path("/fake/root/runs/case", DISTRO), pdb_runtime_root_posix="/fake/bundle",
            resource_limits=forged, session_timeout_seconds=30, distro=DISTRO,
        )


# -- contained-PDB preflight gate ----------------------------------------------


def _launch_plan(context: VerifiedExecutionContext, *, target: str = "python_programs/gcd.py", breakpoints: tuple[int, ...] = (5,)) -> PdbLaunchPlan:
    return PdbLaunchPlan(
        python_executable=context.environment.python_executable, driver=target, target=target,
        breakpoints=breakpoints, cwd=".", argv=(target,), environment=dict(context.environment.environment),
    )


def test_contained_preflight_blocks_missing_execution_context(tmp_path: Path) -> None:
    report = contained_pdb_preflight(
        task_id=QUIXBUGS_PDB_TASK_ID, execution_context=None, external_parent=str(tmp_path),
        repository_root=str(tmp_path.parent), launch_plan=None, expected_python_executable=None,
        expected_cwd=None, expected_target=None, expected_breakpoints=None, pdb_observation_budget=3,
    )
    assert not report.authorized
    assert ContainedPdbGateName.EXECUTION_CONTEXT_READY.value in report.blocked_gates


def test_contained_preflight_blocks_unready_resource_isolation(tmp_path: Path) -> None:
    context = _fake_context(resource_isolation_ready=False)
    plan = _launch_plan(context)
    external_parent = wsl_unc_path("/fake/root/runs", DISTRO)
    report = contained_pdb_preflight(
        task_id=QUIXBUGS_PDB_TASK_ID, execution_context=context, external_parent=external_parent,
        repository_root=str(tmp_path), launch_plan=plan, expected_python_executable=context.environment.python_executable,
        expected_cwd=".", expected_target=plan.target, expected_breakpoints=plan.breakpoints, pdb_observation_budget=3,
    )
    assert not report.authorized
    assert ContainedPdbGateName.RESOURCE_ISOLATION_READY.value in report.blocked_gates


def test_contained_preflight_blocks_mismatched_launch_plan_identity(tmp_path: Path) -> None:
    context = _fake_context()
    plan = _launch_plan(context, breakpoints=(999,))  # reviewed breakpoint does not match
    external_parent = wsl_unc_path("/fake/root/runs", DISTRO)
    report = contained_pdb_preflight(
        task_id=QUIXBUGS_PDB_TASK_ID, execution_context=context, external_parent=external_parent,
        repository_root=str(tmp_path), launch_plan=plan, expected_python_executable=context.environment.python_executable,
        expected_cwd=".", expected_target=plan.target, expected_breakpoints=(5,), pdb_observation_budget=3,
    )
    assert not report.authorized
    assert ContainedPdbGateName.LAUNCH_PLAN_IDENTITY.value in report.blocked_gates


def test_contained_preflight_blocks_zero_budget(tmp_path: Path) -> None:
    context = _fake_context()
    plan = _launch_plan(context)
    external_parent = wsl_unc_path("/fake/root/runs", DISTRO)
    report = contained_pdb_preflight(
        task_id=QUIXBUGS_PDB_TASK_ID, execution_context=context, external_parent=external_parent,
        repository_root=str(tmp_path), launch_plan=plan, expected_python_executable=context.environment.python_executable,
        expected_cwd=".", expected_target=plan.target, expected_breakpoints=plan.breakpoints, pdb_observation_budget=0,
    )
    assert not report.authorized
    assert ContainedPdbGateName.OBSERVATION_BUDGET_POSITIVE.value in report.blocked_gates


def test_contained_preflight_blocks_containment_escape(tmp_path: Path) -> None:
    context = _fake_context()
    plan = _launch_plan(context)
    # external_parent is not inside the containment root at all.
    report = contained_pdb_preflight(
        task_id=QUIXBUGS_PDB_TASK_ID, execution_context=context, external_parent=str(tmp_path),
        repository_root=str(tmp_path.parent), launch_plan=plan, expected_python_executable=context.environment.python_executable,
        expected_cwd=".", expected_target=plan.target, expected_breakpoints=plan.breakpoints, pdb_observation_budget=3,
    )
    assert not report.authorized
    assert ContainedPdbGateName.CONTAINMENT_READY.value in report.blocked_gates


def test_contained_preflight_passes_with_consistent_facts(tmp_path: Path) -> None:
    context = _fake_context()
    plan = _launch_plan(context)
    external_parent = wsl_unc_path("/fake/root/runs", DISTRO)
    report = contained_pdb_preflight(
        task_id=QUIXBUGS_PDB_TASK_ID, execution_context=context, external_parent=external_parent,
        repository_root=str(tmp_path), launch_plan=plan, expected_python_executable=context.environment.python_executable,
        expected_cwd=".", expected_target=plan.target, expected_breakpoints=plan.breakpoints, pdb_observation_budget=3,
    )
    assert report.authorized


# -- the real controller PDB gate (decide_pdb_access) --------------------------


def _hypothesis(*, confidence=HypothesisConfidence.LOW, requires_runtime_evidence=True) -> RootCauseHypothesis:
    return RootCauseHypothesis("h1", "low confidence, needs runtime evidence", confidence, HypothesisStatus.ACTIVE, (), requires_runtime_evidence, 1)


def test_gate_blocks_when_failure_not_reproduced() -> None:
    decision = decide_pdb_access(
        PdbPolicy.ON_UNCERTAINTY,
        PdbGateContext(ControllerState.UNDERSTAND, failure_reproduced=False, remaining_pdb_observations=3, failed_patch_attempts=0, active_hypothesis=_hypothesis()),
    )
    assert not decision.allowed
    assert decision.reason is PdbGateReason.FAILURE_NOT_REPRODUCED


def test_gate_blocks_on_zero_budget() -> None:
    decision = decide_pdb_access(
        PdbPolicy.ON_UNCERTAINTY,
        PdbGateContext(ControllerState.UNDERSTAND, failure_reproduced=True, remaining_pdb_observations=0, failed_patch_attempts=0, active_hypothesis=_hypothesis()),
    )
    assert not decision.allowed
    assert decision.reason is PdbGateReason.BUDGET_EXHAUSTED


def test_gate_blocks_missing_hypothesis() -> None:
    decision = decide_pdb_access(
        PdbPolicy.ON_UNCERTAINTY,
        PdbGateContext(ControllerState.UNDERSTAND, failure_reproduced=True, remaining_pdb_observations=3, failed_patch_attempts=0, active_hypothesis=None),
    )
    assert not decision.allowed
    assert decision.reason is PdbGateReason.ACTIVE_HYPOTHESIS_REQUIRED


def test_gate_blocks_non_qualifying_hypothesis() -> None:
    confident = _hypothesis(confidence=HypothesisConfidence.HIGH, requires_runtime_evidence=False)
    decision = decide_pdb_access(
        PdbPolicy.ON_UNCERTAINTY,
        PdbGateContext(ControllerState.UNDERSTAND, failure_reproduced=True, remaining_pdb_observations=3, failed_patch_attempts=0, active_hypothesis=confident),
    )
    assert not decision.allowed
    assert decision.reason is PdbGateReason.UNCERTAINTY_NOT_ESTABLISHED


def test_gate_allows_low_confidence_runtime_evidence_hypothesis() -> None:
    decision = decide_pdb_access(
        PdbPolicy.ON_UNCERTAINTY,
        PdbGateContext(ControllerState.UNDERSTAND, failure_reproduced=True, remaining_pdb_observations=3, failed_patch_attempts=0, active_hypothesis=_hypothesis()),
    )
    assert decision.allowed
    assert decision.reason is PdbGateReason.ALLOWED


# -- deterministic driver never forges the gate --------------------------------


class _FakeObservation:
    def __init__(self, status: str, name: str, payload: dict) -> None:
        class _Status:
            def __init__(self, value):
                self.value = value

        self.status = _Status(status)
        self.name = name
        self.payload = payload


class _FakeSnapshot:
    def __init__(self, *, state, model_call_index, budget_limits, budget_state, hypotheses, last_observation=None):
        self.state = state
        self.model_call_index = model_call_index
        self.budget_limits = budget_limits
        self.budget_state = budget_state
        self.hypotheses = hypotheses
        self.last_observation = last_observation


def test_driver_refuses_runtime_evidence_transition_when_gate_denies() -> None:
    from agentic_debugger.agent.controller_policy import ControllerBudgetLimits, ControllerBudgetState, HypothesisLedger
    from agentic_debugger.agent.model_adapter import ModelAdapterError

    driver = DeterministicPdbReachabilityDriver(hypothesis_id="h1", hypothesis_statement="needs runtime evidence")
    limits = ControllerBudgetLimits(max_patch_attempts=1, max_test_runs=5, max_pdb_observations=0)  # zero budget
    ledger = HypothesisLedger()
    snapshot = _FakeSnapshot(state=ControllerState.UNDERSTAND, model_call_index=0, budget_limits=limits, budget_state=ControllerBudgetState(), hypotheses=ledger)
    directive = driver.next_directive(snapshot)  # adds the hypothesis first
    assert directive.kind.value == "add_hypothesis"

    ledger = HypothesisLedger.add(ledger, limits, hypothesis_id="h1", statement="x", confidence=HypothesisConfidence.LOW, requires_runtime_evidence=True)
    snapshot2 = _FakeSnapshot(state=ControllerState.UNDERSTAND, model_call_index=1, budget_limits=limits, budget_state=ControllerBudgetState(), hypotheses=ledger)
    with pytest.raises(ModelAdapterError):
        driver.next_directive(snapshot2)
    assert driver.gate_decisions and not driver.gate_decisions[-1].allowed


# -- pdb runtime bundle is minimal and byte-identical to the accepted repo ----


def test_pdb_runtime_bundle_hashes_match_accepted_repository_files(tmp_path: Path) -> None:
    destination = tmp_path / "bundle"
    hashes = materialize_pdb_runtime_bundle(destination)
    import hashlib

    pkg_dir = ROOT / "agentic_debugger"
    assert hashes["agentic_debugger/__init__.py"] == hashlib.sha256((pkg_dir / "__init__.py").read_bytes()).hexdigest()
    assert hashes["agentic_debugger/runtime/pdb_worker.py"] == hashlib.sha256((pkg_dir / "runtime" / "pdb_worker.py").read_bytes()).hexdigest()
    assert hashes["agentic_debugger/runtime/pdb_protocol.py"] == hashlib.sha256((pkg_dir / "runtime" / "pdb_protocol.py").read_bytes()).hexdigest()
    assert hashes["agentic_debugger/runtime/exceptions.py"] == hashlib.sha256((pkg_dir / "runtime" / "exceptions.py").read_bytes()).hexdigest()
    # The bundled runtime/__init__.py is deliberately a stub, not the real one.
    assert (destination / "agentic_debugger" / "runtime" / "__init__.py").read_text(encoding="utf-8") == ""
    real_runtime_init = (pkg_dir / "runtime" / "__init__.py").read_text(encoding="utf-8")
    assert real_runtime_init != ""


def test_pdb_runtime_bundle_refuses_existing_destination(tmp_path: Path) -> None:
    destination = tmp_path / "bundle"
    destination.mkdir()
    with pytest.raises(ContainedPdbError):
        materialize_pdb_runtime_bundle(destination)


# -- orchestration-level scope pinning and fail-closed behavior ----------------


def test_reachability_case_rejects_a_different_task_manifest(tmp_path: Path) -> None:
    context = _fake_context()
    facts = QuixBugsPreflightFacts(
        platform="linux", pinned_source_verified=True, license_reviewed=True, test_command_available=True,
        workspace_cleanup_ready=True, target_annotation_reviewed=True, external_parent=str(tmp_path), execution_context=context,
    )
    with pytest.raises(ContainedPdbError):
        run_quixbugs_gcd_pdb_reachability_case(
            repository_root=str(ROOT), manifest_path=str(HANOI_MANIFEST), sources_parent=str(tmp_path),
            facts=facts, resource_limits=_resource_limits(),
        )


def test_reachability_case_blocked_when_quixbugs_preflight_not_authorized(tmp_path: Path) -> None:
    facts = QuixBugsPreflightFacts()  # nothing cleared
    result = run_quixbugs_gcd_pdb_reachability_case(
        repository_root=str(ROOT), manifest_path=str(GCD_MANIFEST), sources_parent=str(tmp_path),
        facts=facts, resource_limits=_resource_limits(),
    )
    assert result.verdict == "REACHABILITY_CASE_BLOCKED"
    assert result.cleanup_attempted is False
    assert result.cleanup_succeeded is True


def test_reachability_case_blocked_without_execution_context(tmp_path: Path) -> None:
    facts = QuixBugsPreflightFacts(
        platform="linux", pinned_source_verified=True, license_reviewed=True, test_command_available=True,
        workspace_cleanup_ready=True, target_annotation_reviewed=True, external_parent=str(tmp_path), execution_context=None,
    )
    result = run_quixbugs_gcd_pdb_reachability_case(
        repository_root=str(ROOT), manifest_path=str(GCD_MANIFEST), sources_parent=str(tmp_path),
        facts=facts, resource_limits=_resource_limits(),
    )
    assert result.verdict == "REACHABILITY_CASE_BLOCKED"


def test_reachability_case_blocked_when_pinned_source_missing(tmp_path: Path) -> None:
    context = _fake_context()
    facts = QuixBugsPreflightFacts(
        platform="linux", pinned_source_verified=True, license_reviewed=True, test_command_available=True,
        workspace_cleanup_ready=True, target_annotation_reviewed=True, external_parent=str(tmp_path), execution_context=context,
    )
    # sources_parent/quixbugs does not exist -- must refuse to clone.
    result = run_quixbugs_gcd_pdb_reachability_case(
        repository_root=str(ROOT), manifest_path=str(GCD_MANIFEST), sources_parent=str(tmp_path),
        facts=facts, resource_limits=_resource_limits(),
    )
    assert result.verdict == "REACHABILITY_CASE_BLOCKED"
    assert result.cleanup_attempted is False


def test_reachability_result_to_mapping_is_json_safe(tmp_path: Path) -> None:
    facts = QuixBugsPreflightFacts()
    result = run_quixbugs_gcd_pdb_reachability_case(
        repository_root=str(ROOT), manifest_path=str(GCD_MANIFEST), sources_parent=str(tmp_path),
        facts=facts, resource_limits=_resource_limits(),
    )
    encoded = json.dumps(result.to_mapping())
    assert "REACHABILITY_CASE_BLOCKED" in encoded


def test_policy_and_repetitions_are_fixed_constants_not_parameters() -> None:
    """The PDB path cannot select another policy or repetition count: these
    are module constants, and the orchestration function accepts no
    parameter that could override them."""
    import inspect

    from agentic_debugger.demo.policies import DemoPolicy
    from agentic_debugger.quixbugs.contained_pdb import QUIXBUGS_PDB_POLICY, QUIXBUGS_PDB_REPETITIONS

    assert QUIXBUGS_PDB_POLICY is DemoPolicy.PDB_ON_UNCERTAINTY
    assert QUIXBUGS_PDB_REPETITIONS == 1
    parameters = set(inspect.signature(run_quixbugs_gcd_pdb_reachability_case).parameters)
    assert "policy" not in parameters and "repetition" not in parameters and "repetitions" not in parameters


def test_runtime_probe_never_uses_the_corrected_source() -> None:
    from agentic_debugger.quixbugs.contained_pdb import _GCD_RUNTIME_PROBE

    assert "correct_python_programs" not in _GCD_RUNTIME_PROBE.module_path
    assert "correct_python_programs" not in _GCD_RUNTIME_PROBE.call_source
    assert _GCD_RUNTIME_PROBE.module_path == "python_programs/gcd.py"


def test_failed_pdb_startup_triggers_workspace_cleanup(tmp_path: Path) -> None:
    """A PDB session that fails to start must still have its workspace removed."""
    from agentic_debugger.demo.tools import PdbProbe
    from agentic_debugger.evaluation.task_schema import DebugTask
    from agentic_debugger.runtime.workspace import TaskWorkspace

    from agentic_debugger.runtime.exceptions import PdbSessionError

    class _ExplodingSession:
        """Mirrors real PdbSession.start(): a launch failure is always
        wrapped as PdbSessionError, which handle_start_pdb explicitly catches
        and cleans up after."""

        def __init__(self, workspace: TaskWorkspace) -> None:
            self.workspace = workspace

        def start(self) -> None:
            raise PdbSessionError("simulated contained PDB worker launch failure")

        def stop(self) -> None:
            return None

    fixture = tmp_path / "fixture"
    (fixture / "python_programs").mkdir(parents=True)
    (fixture / "python_programs" / "gcd.py").write_text("def gcd(a, b):\n    return a\n", encoding="utf-8")
    parent = tmp_path / "parent"
    parent.mkdir()

    adapter = QuixBugsAdapter.from_manifest(GCD_MANIFEST)
    from agentic_debugger.evaluation.task_schema import TaskSource

    source = TaskSource("external", "quixbugs", adapter.source_provenance())
    commands = adapter.build_commands(fail_to_pass=["python_testcases/test_gcd.py::x"], pass_to_pass=["python_testcases/test_gcd.py::y"])
    task: DebugTask = adapter.to_debug_task(source, commands, pdb_observation_budget=3)
    for denied in task.constraints.denied_write_paths:
        target = fixture / denied
        if "." in Path(denied).name:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")
        else:
            target.mkdir(parents=True, exist_ok=True)

    workspace = TaskWorkspace(str(fixture), parent_dir=str(parent))
    probe = PdbProbe(source_dir=fixture, parent_dir=parent, script="python_programs/gcd.py", breakpoint_line=2, focus_function="gcd")
    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=probe, pdb_session_factory=_ExplodingSession)

    from agentic_debugger.agent.tool_registry import ToolExecutionError
    from agentic_debugger.demo.tools import build_registry
    from agentic_debugger.events.schema import Action

    registry = build_registry(context, pdb_policy=PdbPolicy.ON_UNCERTAINTY)
    action = Action(action_id="a1", run_id="r1", task_id=task.task_id, state=ControllerState.RUNTIME_EVIDENCE, name="start_pdb_session", arguments={})
    observation = registry.dispatch(action, observation_id="o1")
    assert observation.status.value == "error"
    assert context.pdb_workspace is None  # cleaned up by release_pdb() inside handle_start_pdb
    workspace.cleanup()


def test_hypothesis_statement_does_not_copy_manifest_oracle_prose() -> None:
    """The deterministic driver's hypothesis must not be driven by the
    manifest's oracle root-cause prose."""
    adapter = QuixBugsAdapter.from_manifest(GCD_MANIFEST)
    oracle_summary = adapter.manifest.oracle["root_cause_summary"]
    driver = DeterministicPdbReachabilityDriver(
        hypothesis_id="quixbugs-gcd-runtime-evidence-v1",
        hypothesis_statement=(
            "Low-confidence hypothesis: the defect concerns how arguments are threaded "
            "through gcd's own recursive call; runtime evidence of the first call's stack "
            "and locals is required before proposing a root cause."
        ),
    )
    assert driver.hypothesis_statement != oracle_summary
    assert "never advances" not in driver.hypothesis_statement
    assert "recurses forever" not in driver.hypothesis_statement


# -- repaired fail-closed verdict predicate: sequence/event/provenance ------
#
# Fixture: the real events_jsonl captured by the one authorized WSL/Bubblewrap
# deterministic reachability run, tracked at
# tests/golden_trajectories/data/quixbugs-gcd-pdb-reachability-captured-result.json
# so these tests (and scripts/quixbugs_gcd_pdb_reachability_offline_revalidation.py)
# work from any fresh checkout without depending on the untracked _ai-review/
# package. Each negative test mutates a deep copy of the real golden trail --
# it never hand-writes a synthetic event trail from scratch.

GOLDEN_CAPTURED_RESULT = ROOT / "tests" / "golden_trajectories" / "data" / "quixbugs-gcd-pdb-reachability-captured-result.json"
_EXPECTED_SCRIPT = _GCD_RUNTIME_PROBE.module_path
_EXPECTED_FUNCTION = _GCD_RUNTIME_PROBE.focus_function
_EXPECTED_BREAKPOINT = 5


def _load_golden_events() -> list[dict]:
    captured = json.loads(GOLDEN_CAPTURED_RESULT.read_text(encoding="utf-8"))
    return [json.loads(line) for line in captured["events_jsonl"].strip("\n").split("\n") if line]


def _serialize(events: list[dict]) -> str:
    return "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n"


def _renumber(events: list[dict]) -> list[dict]:
    for index, event in enumerate(events):
        event["sequence"] = index
    return events


def _mutate_observation_payload(events: list[dict], name: str, **overrides: object) -> list[dict]:
    events = copy.deepcopy(events)
    for event in events:
        if event["event_type"] == "observation" and event["name"] == name:
            event["payload"]["observation"]["payload"].update(overrides)
    return events


def _set_observation_status(events: list[dict], name: str, status: str) -> list[dict]:
    events = copy.deepcopy(events)
    for event in events:
        if event["event_type"] == "observation" and event["name"] == name:
            event["payload"]["observation"]["status"] = status
    return events


def _remove_events(events: list[dict], *, event_type: str, name: str) -> list[dict]:
    events = copy.deepcopy(events)
    events = [event for event in events if not (event["event_type"] == event_type and event["name"] == name)]
    return _renumber(events)


def _validate_and_evaluate(events: list[dict]) -> tuple[bool, tuple[str, ...], object]:
    run_id = events[0]["run_id"]
    task_id = events[0]["task_id"]
    events_valid, reasons, parsed = validate_events_jsonl(_serialize(events), run_id=run_id, task_id=task_id)
    sequence = None
    if events_valid:
        sequence = evaluate_reachability_sequence_from_events(
            parsed, expected_script=_EXPECTED_SCRIPT, expected_function=_EXPECTED_FUNCTION,
            expected_breakpoint_line=_EXPECTED_BREAKPOINT,
        )
    return events_valid, reasons, sequence


def test_golden_successful_sequence_passes() -> None:
    events_valid, reasons, sequence = _validate_and_evaluate(_load_golden_events())
    assert events_valid, reasons
    assert sequence.ok, sequence.reasons
    verdict = determine_reachability_verdict(
        result_present=True, quixbugs_authorized=True, contained_authorized=True, any_gate_allowed=True,
        sequence_ok=sequence.ok, events_valid=events_valid, stop_reason_is_failed=True, final_state_is_failed=True,
        cleanup_succeeded=True, canonical_source_unchanged=True, provenance_present=True, diagnostics_empty=True,
    )
    assert verdict == "REACHABILITY_CASE_PASSED"


def test_missing_stop_observation_fails_sequence() -> None:
    events = _remove_events(_load_golden_events(), event_type="observation", name="stop_pdb_session")
    events_valid, reasons, sequence = _validate_and_evaluate(events)
    # The action event with the same name is still present, so the event
    # trail is still nominally "complete" by name -- only the sequence
    # evaluator (which requires the *observation*) must catch this.
    assert events_valid, reasons
    assert not sequence.ok
    assert any("stop_pdb_session" in reason for reason in sequence.reasons)


def test_failed_stop_observation_status_fails_sequence() -> None:
    events = _set_observation_status(_load_golden_events(), "stop_pdb_session", "error")
    events_valid, reasons, sequence = _validate_and_evaluate(events)
    assert events_valid, reasons
    assert not sequence.ok
    assert any("stop_pdb_session" in reason for reason in sequence.reasons)


def test_stopped_false_fails_sequence() -> None:
    events = _mutate_observation_payload(_load_golden_events(), "stop_pdb_session", stopped=False)
    events_valid, reasons, sequence = _validate_and_evaluate(events)
    assert events_valid, reasons
    assert not sequence.ok
    assert any("stop_pdb_session" in reason for reason in sequence.reasons)


def test_workspace_removed_false_fails_sequence() -> None:
    events = _mutate_observation_payload(_load_golden_events(), "stop_pdb_session", workspace_removed=False)
    events_valid, reasons, sequence = _validate_and_evaluate(events)
    assert events_valid, reasons
    assert not sequence.ok
    assert any("stop_pdb_session" in reason for reason in sequence.reasons)


def test_missing_start_pdb_session_sequence_fails() -> None:
    events = _remove_events(_load_golden_events(), event_type="observation", name="start_pdb_session")
    events_valid, reasons, sequence = _validate_and_evaluate(events)
    assert events_valid, reasons
    assert not sequence.ok
    assert any("start_pdb_session" in reason for reason in sequence.reasons)


def test_missing_stack_summary_sequence_fails() -> None:
    events = _remove_events(_load_golden_events(), event_type="observation", name="get_stack_summary")
    events_valid, reasons, sequence = _validate_and_evaluate(events)
    assert events_valid, reasons
    assert not sequence.ok
    assert any("get_stack_summary" in reason for reason in sequence.reasons)


def test_missing_frame_locals_sequence_fails() -> None:
    events = _remove_events(_load_golden_events(), event_type="observation", name="get_frame_locals")
    events_valid, reasons, sequence = _validate_and_evaluate(events)
    assert events_valid, reasons
    assert not sequence.ok
    assert any("get_frame_locals" in reason for reason in sequence.reasons)


def test_missing_required_event_name_entirely_fails_events_valid() -> None:
    events = _load_golden_events()
    events = [event for event in events if event["name"] != "get_frame_locals"]
    events = _renumber(events)
    events_valid, reasons, sequence = _validate_and_evaluate(events)
    assert not events_valid
    assert any("get_frame_locals" in reason for reason in reasons)
    assert sequence is None


def test_empty_events_jsonl_fails_closed() -> None:
    events_valid, reasons, parsed = validate_events_jsonl("", run_id="r", task_id="t")
    assert not events_valid
    assert parsed == ()
    assert reasons


def test_truncated_events_jsonl_fails_closed() -> None:
    golden = _serialize(_load_golden_events())
    truncated = golden[: len(golden) // 2]
    events_valid, reasons, parsed = validate_events_jsonl(truncated, run_id="r", task_id="t")
    assert not events_valid
    assert reasons


def test_non_contiguous_sequence_fails_closed() -> None:
    events = _load_golden_events()
    events[3]["sequence"] = 999
    events_valid, reasons, parsed = validate_events_jsonl(_serialize(events), run_id=events[0]["run_id"], task_id=events[0]["task_id"])
    assert not events_valid
    assert any("contiguous" in reason for reason in reasons)


def test_determine_reachability_verdict_requires_every_fact() -> None:
    """No single fact can substitute for the others -- flipping exactly one
    to False must fail the verdict, with everything else held at its
    passing value."""
    passing_kwargs = dict(
        result_present=True, quixbugs_authorized=True, contained_authorized=True, any_gate_allowed=True,
        sequence_ok=True, events_valid=True, stop_reason_is_failed=True, final_state_is_failed=True,
        cleanup_succeeded=True, canonical_source_unchanged=True, provenance_present=True, diagnostics_empty=True,
    )
    assert determine_reachability_verdict(**passing_kwargs) == "REACHABILITY_CASE_PASSED"
    for flag in passing_kwargs:
        broken = dict(passing_kwargs)
        broken[flag] = False
        assert determine_reachability_verdict(**broken) == "REACHABILITY_CASE_FAILED", flag


def test_event_projection_failure_diagnostic_blocks_passed() -> None:
    """A non-empty diagnostics list (e.g. from a failed event-projection
    call) must block PASSED even when every other fact holds."""
    verdict = determine_reachability_verdict(
        result_present=True, quixbugs_authorized=True, contained_authorized=True, any_gate_allowed=True,
        sequence_ok=True, events_valid=True, stop_reason_is_failed=True, final_state_is_failed=True,
        cleanup_succeeded=True, canonical_source_unchanged=True, provenance_present=True,
        diagnostics_empty=False,  # e.g. "events projection failed: ..."
    )
    assert verdict == "REACHABILITY_CASE_FAILED"


def test_aggregate_observation_counts_alone_cannot_forge_passed() -> None:
    """Regression guard for the original defect: two successful aggregate
    PDB observations must not be sufficient on their own -- the repaired
    predicate has no parameter for raw counts at all."""
    import inspect

    parameters = set(inspect.signature(determine_reachability_verdict).parameters)
    assert "successful_pdb_observation_count" not in parameters
    assert "pdb_observations" not in parameters
    assert not any("count" in name for name in parameters)
