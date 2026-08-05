"""Deterministic offline regressions for the paired-pilot v4 budget/verifier path.

Reproduces the recorded v4 failure shapes without provider, network, GPU, or
model calls.  Attempt identity is versioned truth: the preserved
``quixbugs-paired-pilot-v3-attempt-fddf1e39...`` shape (33,685 bytes, 13
provider processes, completed post-apply lifecycle) is V3 evidence replayed
under the v4 terminal contract; the actual V4 attempt is
``quixbugs-paired-pilot-v4-attempt-3b5d7488d61262f97a1c800367ca8e1817e906795dbbf1e04d762c34078e6896``
whose interrupted Case 2 observed 15 provider process attempts, 38,534
cumulative public-evidence bytes, an applied patch with Validate visited, zero
verifier runs, an interrupted controller outcome, no materialized case record,
and an original campaign abort ``BUDGET_EXCEEDED`` because the shape was not
representable.

The fixture case identities are bound to the exact frozen v4 cases recorded by
the preserved campaign record (``campaign.json``) and private transport for
attempt 3b5d7488...: the 26,139-byte malformed/hunk-header-rejection shape
belongs to ``find-in-sorted`` / ``pdb-on-uncertainty`` (order_index 1, the
case the old runner materialized with 10 provider processes, 9 logical calls, 1
retry, and $0.007378 provider-reported cost), and the 38,534-byte
applied-patch interrupted shape belongs to ``find-in-sorted`` /
``static-baseline`` (order_index 2, 15 provider processes, $0.012323) — the
case in flight when the original campaign aborted.

* V4 Case 1 (malformed unified diff, artifact-derived):
  ``quixbugs-find-in-sorted-smoke-v1`` under ``pdb-on-uncertainty``: the
  controller reached the correct diagnosis but every patch attempt was a
  malformed unified diff (hunk header declared old_count=7 while the body
  carries 6 lines); 10 provider processes and 26,139 observed public-evidence
  bytes later no candidate was ever applied and the verifier never ran.  The
  case must materialize as a schema-valid terminal
  (``INFRASTRUCTURE_ERROR`` / controller stage) with ``patch_submissions ==
  0``, ``verifier_runs == 0``, bounded malformed-diff handling, recorded
  disposable-workspace cleanup, the machine-readable budget provenance, and
  the campaign must continue.
* V4 Case 2 (applied patch, Validate visited, interrupted, public evidence
  over budget): ``quixbugs-find-in-sorted-smoke-v1`` under
  ``static-baseline``: a candidate was applied and Validate was reached, the
  run was interrupted, and the frozen public-evidence budget was exceeded
  (38,534 observed bytes).  The terminal result must be schema-valid with
  the exact observed byte count preserved in the machine-readable
  ``budget_exhaustion`` provenance and in the termination detail, the
  counter clamped to the frozen 20,000-byte limit before persistence, the
  applied-patch and verifier accounting preserved, and a typed
  ``ABORTED / INTERRUPTED`` campaign terminal with the interrupted case's
  completed accounting preserved.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

import quixbugs_live_runner_v2 as runner
import quixbugs_opencode_go_adapter as adapter
import quixbugs_paired_pilot as pilot

from agentic_debugger.agent.controller import ControllerRunConfig, DeterministicController, ControllerStopReason
from agentic_debugger.agent.controller_policy import ControllerBudgetLimits, ControllerBudgetState, HypothesisLedger
from agentic_debugger.agent.model_adapter import ControllerSnapshot
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.demo.catalog import build_reference_patch, scenario_for
from agentic_debugger.demo.policies import DemoPolicy, pdb_policy_for
from agentic_debugger.demo.tools import DemoToolContext, build_registry
from agentic_debugger.evaluation.live import (
    LiveCaseResult,
    LiveCaseStatus,
    LiveModelAdapter,
    LiveModelConfig,
    LiveRunLimits,
    ModelRequestBudgetExceeded,
    _finalize_live_case,
)
from agentic_debugger.evaluation.verifier import EvaluationVerifier
from agentic_debugger.runtime.workspace import TaskWorkspace

from test_opencode_go_case_runner import FakeLiveResult, _live_mapping
from test_quixbugs_case_budget_terminal import (
    _completed_post_apply_exhausted_outcome,
    _curated_task,
    _run_campaign_custom,
)
from test_quixbugs_live_runner_v2 import (
    ScriptedCaseRunner,
    _clean_git_state,
    _completed_entries,
    _route_evidence,
    _valid_authorization,
)

CURATED_TASK_ID = "curated-none-handling-001"
MALFORMED_DIFF = "this is not a unified diff at all\njust some prose\n"
V4_ATTEMPT_IDENTITY = "quixbugs-paired-pilot-v4-attempt-3b5d7488d61262f97a1c800367ca8e1817e906795dbbf1e04d762c34078e6896"
ATTEMPT_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "quixbugs_v4_budget_verifier_attempt_fixture.json"


@pytest.fixture
def manifest_v4():
    return pilot.load_manifest(pilot.MANIFEST_PATH_V4)


@pytest.fixture
def auth_v4(manifest_v4, tmp_path):
    return _valid_authorization(
        manifest_v4, tmp_path / "attempt-out",
        campaign_id="quixbugs-paired-pilot-v4",
        campaign_version=4,
        campaign_attempt_identity="quixbugs-paired-pilot-v4-attempt-" + "a" * 64,
    )


@pytest.fixture
def git_state_provider():
    return lambda commit: _clean_git_state(commit)


def _curated_fixture_path() -> Path:
    return REPO_ROOT / "agentic_debugger" / "datasets" / "curated" / CURATED_TASK_ID


def _curated_reference_patch() -> str:
    scenario = scenario_for(CURATED_TASK_ID)
    return build_reference_patch(
        (_curated_fixture_path() / scenario.reference_repair.target_path).read_text(encoding="utf-8"),
        scenario.reference_repair,
    )


def _route_observation(manifest, **overrides) -> dict:
    value = dict(_route_evidence(manifest))
    value.setdefault("preflight_success", True)
    value.update(overrides)
    return value


class _ScriptedTransport:
    """Deterministic provider double: plays one directive per logical call."""

    def __init__(self, directives, *, exhaust_after: int | None = None):
        self.directives = list(directives)
        self.exhaust_after = exhaust_after
        self.calls = 0

    def request(self, payload, timeout_seconds):
        self.calls += 1
        if self.exhaust_after is not None and self.calls > self.exhaust_after:
            raise ModelRequestBudgetExceeded(20_475, 20_000)
        directive = self.directives[min(self.calls - 1, len(self.directives) - 1)]
        return {"directive": directive, "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


class _InnerTransport:
    """Minimal OpenCodeGoTransport stand-in for the outcome mapping."""

    def __init__(self, case_id: str, process_attempts: int = 0):
        self.case_id = case_id
        self.process_attempts = process_attempts
        self.last_process_exit_code = 0 if process_attempts else None
        self.last_timed_out = False
        self.last_provider_error_category = None
        self.reported_costs = []
        self.drift_category = None

    def reported_cost_aggregate(self):
        return None if not self.reported_costs else sum(self.reported_costs)


def _run_controller(transport, *, task=None, policy=DemoPolicy.PDB_ON_UNCERTAINTY, tmp_path=None, registry=None, context=None):
    if task is None:
        task = _curated_task()
    if context is None:
        raise AssertionError("a live tool context is required")
    config = LiveModelConfig("replay-model", ("replay-command",))
    live_adapter = LiveModelAdapter(
        task=task, policy=policy, config=config,
        transport=transport, limits=LiveRunLimits(max_model_requests=16, max_retries=2, max_controller_steps=16),
        registry=registry,
    )
    controller = DeterministicController(registry, live_adapter, ControllerRunConfig(max_model_calls=16))
    result = controller.run(ControllerSnapshot(
        "v4-path-run", task.task_id, ControllerState.REPRODUCE, 0,
        ControllerBudgetLimits.from_task_constraints(task.constraints),
        ControllerBudgetState(), HypothesisLedger(),
    ))
    return result, live_adapter, config


def _finalized_case(task, context, workspace, result, live_adapter, config, *, verify, interrupted=False, campaign_version=4):
    return _finalize_live_case(
        task_id=task.task_id, policy=DemoPolicy.STATIC_BASELINE, repetition=1,
        case_id="v4-path-case", run_id="v4-path-run", config=config, task=task,
        context=context, workspace=workspace, result=result, metrics=live_adapter.metrics,
        live_adapter=live_adapter, started=time.monotonic(), interrupted=interrupted,
        controller_failed=False, diagnostics=[], verify=verify,
        extra_cleanup=lambda: (True, None), extra_cleanup_owned=False,
        evidence={"pdb_gate_decisions": [], "directive_rejections": []},
        campaign_version=campaign_version,
    )


def _materialize_and_validate(manifest, case, outcome, *, auth):
    runner.validate_case_outcome(outcome)
    runner.enforce_case_budgets(outcome, manifest, case_policy=case["policy"])
    record = runner.materialize_case_record(
        manifest, case, auth, _route_observation(manifest), outcome,
        attempt_identity=auth["campaign_attempt_identity"],
        execution_commit=runner.ACCEPTED_BASELINE,
    )
    pilot.validate_case_result(record, manifest, auth)
    return record


# ---- Case 1: malformed unified diff exhausts the patch budget -----------------


def test_malformed_unified_diff_exhausts_patch_budget_without_candidate(tmp_path, manifest_v4, auth_v4):
    """The recorded v4 Case-1 shape: the controller reaches the correct
    diagnosis (hypothesis created, Patch visited), every patch attempt is a
    malformed unified diff, the patch budget is exhausted, no candidate is
    ever applied, and the verifier never runs.  The outcome stays
    schema-valid (INFRASTRUCTURE_ERROR / controller stage) with
    patch_submissions == 0 and verifier_runs == 0, and the disposable
    workspace cleanup is recorded."""
    task = _curated_task()
    case = manifest_v4["case_order"][0]
    directives = [
        {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}},
        {"kind": "transition", "target_state": "Understand", "reason": "reproduced"},
        {"kind": "add_hypothesis", "hypothesis_id": "h-1", "statement": "root cause",
         "confidence": "low", "evidence_refs": [], "requires_runtime_evidence": False},
        {"kind": "transition", "target_state": "Patch", "reason": "diagnosis complete"},
        {"kind": "action", "name": "apply_patch", "arguments": {"patch": MALFORMED_DIFF}},
    ]
    with TaskWorkspace(str(_curated_fixture_path()), parent_dir=str(tmp_path)) as ws:
        context = DemoToolContext(task=task, workspace=ws, patch="", probe=None)
        registry = build_registry(context, pdb_policy=pdb_policy_for(DemoPolicy.PDB_ON_UNCERTAINTY))
        transport = _ScriptedTransport(directives)
        result, live_adapter, config = _run_controller(
            transport, task=task, registry=registry, context=context,
        )

        assert result.stop_reason is ControllerStopReason.BUDGET_EXHAUSTED
        assert result.final_state is ControllerState.FAILED
        assert context.patch_applied is False
        assert context.candidate_patch == ""
        assert context.patch_changed_files == ()

        finalized = _finalized_case(
            task, context, ws, result, live_adapter, config,
            verify=lambda: pytest.fail("the verifier must never run for a malformed-patch case"),
        )
        assert finalized.status is LiveCaseStatus.CONTROLLER_FAILED

        inner = _InnerTransport("v4-case-1", process_attempts=0)
        inner.process_attempts = result.model_calls
        outcome = adapter._outcome_from_live_case(
            case, FakeLiveResult(finalized.to_mapping()), _route_observation(manifest_v4), inner,
            transport_attempts=result.model_calls, policy_value="pdb-on-uncertainty",
            run_id="v4-case-1",
            source_hash=next(item["source_sha256"] for item in manifest_v4["inventory"] if item["task_id"] == case["task_id"]),
        )

        assert outcome["terminal_status"] == "INFRASTRUCTURE_ERROR"
        assert outcome["terminal_reason_code"] == "INFRASTRUCTURE_FAILURE"
        assert outcome["infrastructure_evidence"]["stage"] == "controller"
        assert outcome["infrastructure_evidence"]["reason_code"] == "CONTROLLER_FAILURE"
        assert outcome["infrastructure_evidence"]["prior_lifecycle_completed"] is True
        assert outcome["patch_submissions"] == 0
        assert outcome["verifier_runs"] == 0
        assert outcome["candidate_hash"] is None
        assert outcome.get("candidate_provenance") is None
        assert outcome["repair_outcome"] == "NO_CANDIDATE"
        assert outcome["interrupted"] is False
        assert outcome["logical_model_calls"] == 7
        assert outcome["provider_process_attempts"] == 7
        assert outcome["valid_directives"] == 7
        assert outcome["hypotheses_created"] == 1
        assert outcome["baseline_reproduction"] is True
        assert outcome["controller_states_visited"] == ["Reproduce", "Understand", "Patch", "Failed"]
        assert outcome["owned_workspace_cleanup"] is True
        assert outcome["public_request_hash"] is not None
        assert outcome["source_hash"] is not None
        assert outcome["terminal_transport_evidence"]["final_attempt_classification"] == "INFRASTRUCTURE_FAILURE"
        assert outcome["terminal_transport_evidence"]["provider_completed_response"] is True

        record = _materialize_and_validate(manifest_v4, case, outcome, auth=auth_v4)
        assert record["patch_submissions"] == 0
        assert record["verifier_runs"] == 0


def test_malformed_patch_exhaustion_case_materializes_and_campaign_continues(manifest_v4, auth_v4, tmp_path, git_state_provider):
    """Campaign level: case 1 produces the adapter-shaped malformed-patch
    exhaustion outcome; the case materializes as a schema-valid
    INFRASTRUCTURE_ERROR terminal and the campaign continues to the remaining
    frozen cases."""
    route = _route_observation(manifest_v4)
    case = manifest_v4["case_order"][0]
    entries = _completed_entries(manifest_v4)
    entries[0] = {
        "provider_process_attempts": 7,
        "outcome": {
            "terminal_status": "INFRASTRUCTURE_ERROR",
            "terminal_reason_code": "INFRASTRUCTURE_FAILURE",
            "termination_reason": "controller transitioned to Failed after patch budget exhausted (malformed unified diff)",
            "logical_model_calls": 7,
            "provider_process_attempts": 7,
            "retries": 0,
            "valid_directives": 7,
            "malformed_directive_rejections": 0,
            "bounded_directive_feedback_events": 0,
            "baseline_reproduction": True,
            "controller_states_visited": ["Reproduce", "Understand", "Patch", "Failed"],
            "hypotheses_created": 1,
            "pdb_gate_decisions": [],
            "pdb_counts": dict(runner.ZERO_PDB_COUNTS),
            "pdb_sessions_started": 0,
            "successful_pdb_observations": 0,
            "failed_pdb_observations": 0,
            "verifier_runs": 0,
            "patch_submissions": 0,
            "independent_verifier_result": {"status": "NOT_RUN", "outcome": None, "lifecycle_succeeded": False},
            "transport_evidence": {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False},
            "terminal_transport_evidence": {
                "final_attempt_classification": "INFRASTRUCTURE_FAILURE", "process_exit_code": 0,
                "timed_out": False, "provider_error_category": None, "provider_completed_response": True,
                "evidence_reference": f"opencode-go:{case['case_id']}:7",
            },
            "blocked_evidence": {"block_kind": "none", "reason_code": "NONE", "confirmed": False, "evidence_reference": "v4:none"},
            "infrastructure_evidence": {
                "stage": "controller", "reason_code": "CONTROLLER_FAILURE", "confirmed_failure": True,
                "classification": "CONTROLLER", "terminal_classification": "INFRASTRUCTURE_FAILURE",
                "provider_attempt_index": None, "prior_lifecycle_completed": True,
                "source_mutation_observed": False, "expected_source_hash": None,
                "evidence_reference": "v4:controller",
            },
            "preflight_failure_evidence": {field: None for field in pilot.ALL_PREFLIGHT_FAILURE_FIELDS},
            "campaign_stop_evidence": {field: None for field in pilot.CAMPAIGN_STOP_EVIDENCE_FIELDS},
            "prompt_tokens": 1200, "completion_tokens": 400, "reasoning_tokens": 0,
            "provider_reported_cost": route["provider_reported_cost"],
            "wall_clock_duration_seconds": 90.0,
            "public_evidence_bytes": 10162,
            "canonical_source_restoration": True,
            "owned_workspace_cleanup": True,
            "evidence_consistency": True,
            "public_request_hash": "b" * 64,
            "source_hash": next(i["source_sha256"] for i in manifest_v4["inventory"] if i["task_id"] == case["task_id"]),
            "candidate_hash": None,
            "repair_outcome": "NO_CANDIDATE",
            "resource_ids": {},
            "interrupted": False,
        },
    }

    record, factory, case_runner, output = _run_campaign_custom(
        manifest_v4, auth_v4, tmp_path,
        case_runner=ScriptedCaseRunner(entries),
        runner_entries=entries,
        git_state_provider=git_state_provider,
    )

    assert record["status"] == "COMPLETED"
    assert record["stop_reason"] is None
    assert record["counts"]["completed_case_count"] == 6
    assert record["counts"]["aborted_case_count"] == 0
    first = record["cases"][0]
    assert first["terminal_status"] == "INFRASTRUCTURE_ERROR"
    assert first["infrastructure_evidence"]["stage"] == "controller"
    assert first["patch_submissions"] == 0
    assert first["verifier_runs"] == 0
    assert first["interrupted"] is False
    assert runner.verify_attempt_package(output, manifest_v4)["consistent"] is True
    runner.validate_campaign_record(record, manifest_v4)


# ---- Case 2: applied patch, Validate reached, verifier/budget path ------------


def test_applied_patch_reaches_validate_and_verifier_when_budget_remains(tmp_path, manifest_v4):
    """Requirement: an applied patch must be able to reach Validate and the
    independent verifier when budget remains.  The real controller drives the
    curated fixture to Done with an applied candidate, the independent
    verifier executes and confirms RESOLVED, and the v4 verifier-authoritative
    classification materializes a schema-valid RESOLVED case."""
    task = _curated_task()
    patch = _curated_reference_patch()
    directives = [
        {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}},
        {"kind": "transition", "target_state": "Understand", "reason": "reproduced"},
        {"kind": "add_hypothesis", "hypothesis_id": "h-1", "statement": "root cause",
         "confidence": "low", "evidence_refs": [], "requires_runtime_evidence": False},
        {"kind": "transition", "target_state": "Patch", "reason": "diagnosis complete"},
        {"kind": "action", "name": "apply_patch", "arguments": {"patch": patch}},
        {"kind": "action", "name": "syntax_check", "arguments": {}},
        {"kind": "transition", "target_state": "Validate", "reason": "patch applied"},
        {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "post_patch"}},
        {"kind": "action", "name": "run_regression_tests", "arguments": {}},
        {"kind": "action", "name": "classify_outcome", "arguments": {}},
        {"kind": "transition", "target_state": "Done", "reason": "finished"},
    ]
    case = manifest_v4["case_order"][1]
    with TaskWorkspace(str(_curated_fixture_path()), parent_dir=str(tmp_path)) as ws:
        context = DemoToolContext(task=task, workspace=ws, patch="", probe=None)
        registry = build_registry(context, pdb_policy=pdb_policy_for(DemoPolicy.STATIC_BASELINE))
        transport = _ScriptedTransport(directives)
        result, live_adapter, config = _run_controller(
            transport, task=task, policy=DemoPolicy.STATIC_BASELINE, registry=registry, context=context,
        )
        assert result.stop_reason is ControllerStopReason.DONE
        assert result.final_state is ControllerState.DONE
        assert context.patch_applied is True

        finalized = _finalized_case(
            task, context, ws, result, live_adapter, config,
            verify=lambda: EvaluationVerifier(
                str(REPO_ROOT), workspace_parent=str(tmp_path)
            ).evaluate(task, context.candidate_patch),
        )
        assert finalized.status is LiveCaseStatus.RESOLVED

        inner = _InnerTransport("v4-case-2", process_attempts=result.model_calls)
        outcome = adapter._outcome_from_live_case(
            case, FakeLiveResult(finalized.to_mapping()), _route_observation(manifest_v4), inner,
            transport_attempts=result.model_calls, policy_value="static-baseline",
            run_id="v4-case-2", source_hash="0" * 64,
        )
        assert outcome["terminal_status"] == "RESOLVED"
        assert outcome["patch_submissions"] == 1
        assert outcome["verifier_runs"] == 1
        assert outcome.get("candidate_provenance") == "verifier_record"
        assert outcome["repair_outcome"] == "RESOLVED"
        assert outcome["interrupted"] is False
        assert outcome["owned_workspace_cleanup"] is True
        assert outcome["public_evidence_bytes"] <= manifest_v4["budgets"]["max_public_evidence_bytes"]
        runner.validate_case_outcome(outcome)
        runner.enforce_case_budgets(outcome, manifest_v4, case_policy="static-baseline")


def test_validate_visited_budget_stop_classifies_validation_not_reached(tmp_path, manifest_v4):
    """The Case-2 pre-verifier shape: a candidate was applied and Validate was
    reached, then the next public request exceeded the frozen budget before
    the verifier ran.  The real controller chain classifies the case as
    VALIDATION_NOT_REACHED with the applied candidate's provenance preserved
    and the verifier never executed."""
    task = _curated_task()
    patch = _curated_reference_patch()
    directives = [
        {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}},
        {"kind": "transition", "target_state": "Understand", "reason": "reproduced"},
        {"kind": "add_hypothesis", "hypothesis_id": "h-1", "statement": "root cause",
         "confidence": "low", "evidence_refs": [], "requires_runtime_evidence": False},
        {"kind": "transition", "target_state": "Patch", "reason": "diagnosis complete"},
        {"kind": "action", "name": "apply_patch", "arguments": {"patch": patch}},
        {"kind": "transition", "target_state": "Validate", "reason": "patch applied"},
    ]
    case = manifest_v4["case_order"][1]
    with TaskWorkspace(str(_curated_fixture_path()), parent_dir=str(tmp_path)) as ws:
        context = DemoToolContext(task=task, workspace=ws, patch="", probe=None)
        registry = build_registry(context, pdb_policy=pdb_policy_for(DemoPolicy.STATIC_BASELINE))
        transport = _ScriptedTransport(directives, exhaust_after=len(directives))
        result, live_adapter, config = _run_controller(
            transport, task=task, policy=DemoPolicy.STATIC_BASELINE, registry=registry, context=context,
        )
        assert result.stop_reason is ControllerStopReason.MODEL_ERROR
        assert context.patch_applied is True
        assert live_adapter.metrics.termination_reason == "public_evidence_budget_exceeded"

        finalized = _finalized_case(
            task, context, ws, result, live_adapter, config,
            verify=lambda: pytest.fail("the verifier must not run on a budget stop"),
        )
        assert finalized.status is LiveCaseStatus.VALIDATION_NOT_REACHED

        inner = _InnerTransport("v4-case-2b", process_attempts=result.model_calls)
        outcome = adapter._outcome_from_live_case(
            case, FakeLiveResult(finalized.to_mapping()), _route_observation(manifest_v4), inner,
            transport_attempts=result.model_calls, policy_value="static-baseline",
            run_id="v4-case-2b", source_hash="0" * 64,
        )
        assert outcome["terminal_status"] == "VALIDATION_NOT_REACHED"
        assert outcome["terminal_reason_code"] == "VALIDATION_NOT_REACHED_PRE_VALIDATE"
        assert outcome["patch_submissions"] == 1
        assert outcome.get("candidate_provenance") == "applied_patch_event"
        assert outcome["verifier_runs"] == 0
        assert outcome["candidate_hash"] and len(outcome["candidate_hash"]) == 64
        assert outcome["repair_outcome"] == "NO_CANDIDATE"
        assert outcome["interrupted"] is False
        runner.validate_case_outcome(outcome)
        runner.enforce_case_budgets(outcome, manifest_v4, case_policy="static-baseline")


def test_v3_attempt_fddf1e39_post_apply_exhaustion_clamps_under_v4_contract(manifest_v4, auth_v4, tmp_path, git_state_provider):
    """The preserved V3 attempt fddf1e39... shape (12 logical calls, 13
    provider process attempts, 1 bounded retry, 1 applied candidate, 1
    verifier run, 33,685 cumulative public evidence bytes) replayed under the
    v4 contract terminalizes as RESOLVED with the exact byte count preserved
    in the machine-readable budget_exhaustion provenance and in the
    termination detail, the counter clamped to the frozen 20,000-byte limit
    before the record is persisted, all accounting preserved, and the
    campaign continuing."""
    case = manifest_v4["case_order"][0]
    route = _route_observation(manifest_v4)
    outcome = _completed_post_apply_exhausted_outcome(manifest_v4, case, route)

    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(outcome, manifest_v4, case_policy=case["policy"])
    assert info.value.observed == 33685
    assert info.value.limit == 20000

    rewritten = runner._budget_exhausted_outcome(case, outcome, info.value, run_id="replay-fddf1e39", manifest=manifest_v4)
    assert rewritten is not None
    assert rewritten["terminal_status"] == "RESOLVED"
    assert rewritten["public_evidence_bytes"] == 20000
    assert "33685" in rewritten["termination_reason"]
    assert rewritten["logical_model_calls"] == 12
    assert rewritten["provider_process_attempts"] == 13
    assert rewritten["retries"] == 1
    assert rewritten["patch_submissions"] == 1
    assert rewritten["verifier_runs"] == 1
    assert rewritten.get("candidate_provenance") == "verifier_record"
    assert rewritten["interrupted"] is False
    assert rewritten["budget_exhaustion"] == {
        "configured_limit": 20000, "observed_bytes": 33685, "persisted_bytes": 20000,
        "state": "exhausted", "truncated": True,
    }
    runner.validate_budget_exhaustion_provenance(rewritten["budget_exhaustion"], budgets=manifest_v4["budgets"])

    record = _materialize_and_validate(manifest_v4, case, rewritten, auth=auth_v4)
    assert record["public_evidence_bytes"] == 20000
    assert record["budget_exhaustion"]["observed_bytes"] == 33685
    assert "33685" in record["termination_reason"]

    entries = _completed_entries(manifest_v4)
    entries[0] = {"provider_process_attempts": 13, "outcome": outcome}
    campaign_record, factory, case_runner, output = _run_campaign_custom(
        manifest_v4, auth_v4, tmp_path,
        case_runner=ScriptedCaseRunner(entries),
        runner_entries=entries,
        git_state_provider=git_state_provider,
    )
    assert campaign_record["status"] == "COMPLETED"
    first = campaign_record["cases"][0]
    assert first["terminal_status"] == "RESOLVED"
    assert first["public_evidence_bytes"] == 20000
    assert first["budget_exhaustion"]["observed_bytes"] == 33685
    assert first["budget_exhaustion"]["state"] == "exhausted"
    assert first["budget_exhaustion"]["truncated"] is True
    assert first["logical_model_calls"] == 12
    assert first["provider_process_attempts"] == 13
    assert first["retries"] == 1
    assert first["patch_submissions"] == 1
    assert first["verifier_runs"] == 1
    assert first["interrupted"] is False
    assert runner.verify_attempt_package(output, manifest_v4)["consistent"] is True
    runner.validate_campaign_record(campaign_record, manifest_v4)


# ---- interruption: clean schema-valid representation --------------------------


def _interrupted_outcome(manifest, case, *, patch_applied: bool = True, verifier_runs: int = 0):
    source_hash = next(item["source_sha256"] for item in manifest["inventory"] if item["task_id"] == case["task_id"])
    outcome = {
        "terminal_status": "INFRASTRUCTURE_ERROR",
        "terminal_reason_code": "INFRASTRUCTURE_FAILURE",
        "termination_reason": "opencode-go adapter: INCOMPLETE: interrupted",
        "logical_model_calls": 5,
        "provider_process_attempts": 5,
        "retries": 0,
        "valid_directives": 5,
        "malformed_directive_rejections": 0,
        "bounded_directive_feedback_events": 0,
        "baseline_reproduction": True,
        "controller_states_visited": ["Reproduce", "Understand", "Patch", "Validate", "Failed"],
        "hypotheses_created": 1,
        "pdb_gate_decisions": [],
        "pdb_counts": dict(runner.ZERO_PDB_COUNTS),
        "pdb_sessions_started": 0,
        "successful_pdb_observations": 0,
        "failed_pdb_observations": 0,
        "verifier_runs": verifier_runs,
        "patch_submissions": 1 if patch_applied else 0,
        "candidate_provenance": "applied_patch_event" if patch_applied else None,
        "independent_verifier_result": {"status": "NOT_RUN", "outcome": None, "lifecycle_succeeded": False},
        "transport_evidence": {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False},
        "terminal_transport_evidence": {
            "final_attempt_classification": "INFRASTRUCTURE_FAILURE", "process_exit_code": 0,
            "timed_out": False, "provider_error_category": None, "provider_completed_response": True,
            "evidence_reference": f"opencode-go:{case['case_id']}:5",
        },
        "blocked_evidence": {"block_kind": "none", "reason_code": "NONE", "confirmed": False, "evidence_reference": "v4:none"},
        "infrastructure_evidence": {
            "stage": "controller", "reason_code": "CONTROLLER_FAILURE", "confirmed_failure": True,
            "classification": "CONTROLLER", "terminal_classification": "INFRASTRUCTURE_FAILURE",
            "provider_attempt_index": None, "prior_lifecycle_completed": True,
            "source_mutation_observed": False, "expected_source_hash": None,
            "evidence_reference": "v4:interrupted",
        },
        "preflight_failure_evidence": {field: None for field in pilot.ALL_PREFLIGHT_FAILURE_FIELDS},
        "campaign_stop_evidence": {field: None for field in pilot.CAMPAIGN_STOP_EVIDENCE_FIELDS},
        "prompt_tokens": 900, "completion_tokens": 300, "reasoning_tokens": 0,
        "provider_reported_cost": 0.0042,
        "wall_clock_duration_seconds": 40.0,
        "public_evidence_bytes": 5000,
        "canonical_source_restoration": True,
        "owned_workspace_cleanup": True,
        "evidence_consistency": True,
        "public_request_hash": "d" * 64,
        "source_hash": source_hash,
        "candidate_hash": "e" * 64 if patch_applied else None,
        "repair_outcome": "NO_CANDIDATE",
        "resource_ids": {},
        "interrupted": True,
    }
    return outcome


def test_interrupted_case_records_patch_applied_and_verifier_state(manifest_v4, auth_v4):
    """An operator-interrupted run (INCOMPLETE live result) keeps its
    interrupted identity and its applied-patch / verification accounting:
    patch_submissions and verifier_runs stay accurate in the schema-valid
    terminal."""
    case = manifest_v4["case_order"][0]
    outcome = _interrupted_outcome(manifest_v4, case, patch_applied=True, verifier_runs=0)
    assert outcome["interrupted"] is True
    assert outcome["patch_submissions"] == 1
    assert outcome["candidate_provenance"] == "applied_patch_event"
    assert outcome["verifier_runs"] == 0
    record = _materialize_and_validate(manifest_v4, case, outcome, auth=auth_v4)
    assert record["interrupted"] is True
    assert record["patch_submissions"] == 1
    assert record["verifier_runs"] == 0


def test_interrupted_case_stops_campaign_with_typed_aborted_terminal(manifest_v4, auth_v4, tmp_path, git_state_provider):
    """The campaign honors an operator interrupt: the interrupted case
    materializes with its completed accounting, the remaining frozen cases
    stay unstarted, and the campaign terminalizes as a typed ABORTED /
    INTERRUPTED package that verifies."""
    entries = _completed_entries(manifest_v4)
    entries[0] = {
        "provider_process_attempts": 5,
        "outcome": _interrupted_outcome(manifest_v4, manifest_v4["case_order"][0]),
    }

    record, factory, case_runner, output = _run_campaign_custom(
        manifest_v4, auth_v4, tmp_path,
        case_runner=ScriptedCaseRunner(entries),
        runner_entries=entries,
        git_state_provider=git_state_provider,
    )

    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "INTERRUPTED"
    assert record["case_lifecycle_states"][manifest_v4["case_order"][0]["case_id"]] == "completed"
    assert record["counts"]["completed_case_count"] == 1
    assert record["counts"]["unstarted_case_count"] == 5
    assert record["counts"]["aborted_case_count"] == 0
    first = record["cases"][0]
    assert first["terminal_status"] == "INFRASTRUCTURE_ERROR"
    assert first["interrupted"] is True
    assert first["patch_submissions"] == 1
    assert first["verifier_runs"] == 0
    runner.validate_campaign_record(record, manifest_v4)
    verification = runner.verify_attempt_package(output, manifest_v4)
    assert verification["consistent"] is True
    assert verification["case_files_on_disk"] == 1
    assert verification["case_records_referenced"] == 1


def test_raw_keyboard_interrupt_produces_typed_aborted_package(manifest_v4, auth_v4, tmp_path, git_state_provider):
    """A raw KeyboardInterrupt escaping the case runner (for example during a
    provider process wait) no longer escapes the campaign: it terminalizes as
    a typed ABORTED / INTERRUPTED package with the in-flight case marked
    aborted and no traceback."""
    entries = _completed_entries(manifest_v4)
    entries[0] = {"provider_process_attempts": 0, "outcome": None, "runner_raises": KeyboardInterrupt()}

    record, factory, case_runner, output = _run_campaign_custom(
        manifest_v4, auth_v4, tmp_path,
        case_runner=ScriptedCaseRunner(entries),
        runner_entries=entries,
        git_state_provider=git_state_provider,
    )

    assert record["status"] == "ABORTED"
    assert record["stop_reason"] == "INTERRUPTED"
    assert record["case_lifecycle_states"][manifest_v4["case_order"][0]["case_id"]] == "aborted"
    assert record["counts"]["aborted_case_count"] == 1
    assert record["counts"]["unstarted_case_count"] == 5
    assert record["counts"]["completed_case_count"] == 0
    assert record["cases"] == []
    runner.validate_campaign_record(record, manifest_v4)
    verification = runner.verify_attempt_package(output, manifest_v4)
    assert verification["consistent"] is True
    assert verification["terminal_commit"] == "PRESENT"


# ---- zero-contact pre-provider shape stays schema-valid -----------------------


def test_pre_provider_harness_error_shape_is_schema_valid(manifest_v4, auth_v4, tmp_path):
    """A zero-contact pre-provider case (harness error, preflight-blocked, or
    interrupted during setup) must not claim public-request or source
    identity: the adapter nulls both hashes so the frozen pre-provider
    INFRASTRUCTURE_ERROR representation validates instead of aborting the
    campaign."""
    case = manifest_v4["case_order"][1]
    source_hash = next(item["source_sha256"] for item in manifest_v4["inventory"] if item["task_id"] == case["task_id"])
    mapping = _live_mapping(manifest_v4, case, **{
        "status": "HARNESS_ERROR",
        "controller": {"completed": False, "final_state": None, "stop_reason": None, "model_calls": 0, "exception": False},
        "verifier": {"executed": False, "failure": False, "status": None, "outcome": None,
                     "patch_application": None, "localization": {"outcome": "NO_LOCALIZATION"}},
        "measurements": {
            "model_request_count": 0, "model_response_count": 0, "retry_count": 0,
            "provider_error_count": 0, "provider_error_kinds": [],
            "token_usage": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
                            "provider_reported": False, "missing_fields": []},
            "termination_reason": None,
            "successful_pdb_observation_count": 0, "failed_pdb_observation_count": 0,
            "tool_call_count": 0, "case_elapsed_duration_ms": 0,
            "model_phase_elapsed_duration_ms": 0, "model_transport_duration_ms": 0,
            "elapsed_scope": "case_observed; model_phase=transport_only",
        },
        "events_jsonl": "",
        "evidence": {"pdb_gate_decisions": [], "directive_rejections": []},
    })
    inner = _InnerTransport("v4-pre-provider", process_attempts=0)
    outcome = adapter._outcome_from_live_case(
        case, FakeLiveResult(mapping), _route_observation(manifest_v4), inner,
        transport_attempts=0, policy_value="static-baseline",
        run_id="v4-pre-provider", source_hash=source_hash,
    )
    assert outcome["terminal_status"] == "INFRASTRUCTURE_ERROR"
    assert outcome["infrastructure_evidence"]["stage"] == "pre_provider"
    assert outcome["infrastructure_evidence"]["reason_code"] == "WORKSPACE_FAILURE"
    assert outcome["source_hash"] is None
    assert outcome["public_request_hash"] is None
    assert outcome["interrupted"] is False
    record = _materialize_and_validate(manifest_v4, case, outcome, auth=auth_v4)
    assert record["source_hash"] is None
    assert record["public_request_hash"] is None


# ---- actual V4 attempt replay (quixbugs-paired-pilot-v4-attempt-3b5d7488...) --


def _fixture_attempt() -> dict:
    import json

    return json.loads(ATTEMPT_FIXTURE_PATH.read_text(encoding="utf-8"))


def _resolve_fixture_case(manifest, observed) -> dict:
    """Resolve the exact frozen case for one recorded fixture case and assert
    every identity field (case_id, order_index, task_id, policy) matches the
    live v4 manifest."""
    frozen = {case["case_id"]: case for case in manifest["case_order"]}
    case_id = observed["case_id"]
    assert case_id in frozen, f"fixture case_id {case_id!r} is not a frozen v4 case"
    case = frozen[case_id]
    assert case["order_index"] == observed["order_index"], (case["order_index"], observed["order_index"])
    assert case["task_id"] == observed["task_id"], (case["task_id"], observed["task_id"])
    assert case["policy"] == observed["policy"], (case["policy"], observed["policy"])
    return case


def _fixture_outcome(manifest, case, observed, *, interrupted: bool, patch_applied: bool) -> dict:
    """Build the raw adapter-shaped outcome for one recorded V4 case from the
    sanitized attempt fixture (aggregate accounting only)."""
    source_hash = next(item["source_sha256"] for item in manifest["inventory"] if item["task_id"] == case["task_id"])
    attempts = observed["provider_process_attempts"]
    validate_visited = bool(observed.get("validate_visited"))
    states = ["Reproduce", "Understand", "Patch", "Validate", "Failed"] if validate_visited else ["Reproduce", "Understand", "Patch", "Failed"]
    return {
        "terminal_status": "INFRASTRUCTURE_ERROR",
        "terminal_reason_code": "INFRASTRUCTURE_FAILURE",
        "termination_reason": "opencode-go adapter: " + ("INCOMPLETE: interrupted" if interrupted else "CONTROLLER_FAILED: controller transitioned to Failed after patch budget exhausted (malformed unified diff)"),
        "logical_model_calls": observed["logical_model_calls"],
        "provider_process_attempts": attempts,
        "retries": observed["retries"],
        "valid_directives": observed["valid_directives"],
        "malformed_directive_rejections": observed["malformed_directive_rejections"],
        "bounded_directive_feedback_events": observed["malformed_directive_rejections"],
        "baseline_reproduction": True,
        "controller_states_visited": states,
        "hypotheses_created": 1,
        "pdb_gate_decisions": [],
        "pdb_counts": dict(runner.ZERO_PDB_COUNTS),
        "pdb_sessions_started": 0,
        "successful_pdb_observations": 0,
        "failed_pdb_observations": 0,
        "verifier_runs": observed["verifier_runs"],
        "patch_submissions": 1 if patch_applied else 0,
        "candidate_provenance": "applied_patch_event" if patch_applied else None,
        "independent_verifier_result": {"status": "NOT_RUN", "outcome": None, "lifecycle_succeeded": False},
        "transport_evidence": {"completed_response": True, "malformed_response": False, "provider_error": False, "synthetic": False},
        "terminal_transport_evidence": {
            "final_attempt_classification": "INFRASTRUCTURE_FAILURE", "process_exit_code": 0,
            "timed_out": False, "provider_error_category": None, "provider_completed_response": True,
            "evidence_reference": f"opencode-go:{case['case_id']}:{attempts}",
        },
        "blocked_evidence": {"block_kind": "none", "reason_code": "NONE", "confirmed": False, "evidence_reference": "v4:none"},
        "infrastructure_evidence": {
            "stage": "controller", "reason_code": "CONTROLLER_FAILURE", "confirmed_failure": True,
            "classification": "CONTROLLER", "terminal_classification": "INFRASTRUCTURE_FAILURE",
            "provider_attempt_index": None, "prior_lifecycle_completed": True,
            "source_mutation_observed": False, "expected_source_hash": None,
            "evidence_reference": "v4:controller",
        },
        "preflight_failure_evidence": {field: None for field in pilot.ALL_PREFLIGHT_FAILURE_FIELDS},
        "campaign_stop_evidence": {field: None for field in pilot.CAMPAIGN_STOP_EVIDENCE_FIELDS},
        "prompt_tokens": 2500, "completion_tokens": 700, "reasoning_tokens": 0,
        "provider_reported_cost": 0.0075,
        "wall_clock_duration_seconds": 150.0,
        "public_evidence_bytes": observed["public_evidence_bytes"],
        "canonical_source_restoration": True,
        "owned_workspace_cleanup": True,
        "evidence_consistency": True,
        "public_request_hash": "f" * 64,
        "source_hash": source_hash,
        "candidate_hash": "c" * 64 if patch_applied else None,
        "repair_outcome": "NO_CANDIDATE",
        "resource_ids": {},
        "interrupted": interrupted,
    }


def test_attempt_fixture_is_sanitized_and_identifies_v4_attempt():
    """The artifact fixture carries only aggregate accounting, the frozen
    budget, and the exact frozen case identities; no credentials, raw
    provider output, or private material."""
    blob = ATTEMPT_FIXTURE_PATH.read_text(encoding="utf-8")
    for marker in ("auth.json", "api_key", "bearer", "sessionID", "step_start", "provider_stdout",
                   "provider_stderr", "usage", "cost", "gold", "oracle", "correct_python_programs"):
        assert marker not in blob, f"private marker leaked: {marker}"
    attempt = _fixture_attempt()
    assert attempt["attempt_identity"] == V4_ATTEMPT_IDENTITY
    assert set(attempt) == {"attempt_identity", "budget", "case1_observed", "case2_observed"}
    assert attempt["budget"]["max_public_evidence_bytes"] == 20000
    for label in ("case1_observed", "case2_observed"):
        for field in ("case_id", "order_index", "task_id", "policy"):
            assert field in attempt[label], f"{label} is missing identity field {field}"
    assert attempt["case1_observed"]["case_id"] != attempt["case2_observed"]["case_id"]
    assert attempt["case1_observed"]["public_evidence_bytes"] == 26139
    assert attempt["case2_observed"]["public_evidence_bytes"] == 38534
    assert attempt["case2_observed"]["interrupted"] is True


def test_v4_fixture_cases_bind_to_exact_frozen_identities(manifest_v4):
    """Both recorded fixture cases resolve to distinct frozen v4 cases and
    every identity field (case_id, order_index, task_id, policy) matches the
    live v4 manifest exactly; neither case is selected by a positional
    default."""
    attempt = _fixture_attempt()
    frozen = {case["case_id"]: case for case in manifest_v4["case_order"]}
    for label in ("case1_observed", "case2_observed"):
        observed = attempt[label]
        case = _resolve_fixture_case(manifest_v4, observed)
        assert case["case_id"] == observed["case_id"]
        assert case["order_index"] == observed["order_index"]
        assert case["task_id"] == observed["task_id"]
        assert case["policy"] == observed["policy"]
    case1 = frozen[attempt["case1_observed"]["case_id"]]
    case2 = frozen[attempt["case2_observed"]["case_id"]]
    assert case1["case_id"] != case2["case_id"]
    assert case1["order_index"] != case2["order_index"]


def test_v4_attempt_3b5d7488_interrupted_budget_exhaustion_end_to_end(manifest_v4, auth_v4, tmp_path, git_state_provider):
    """The actual V4 Case 2 shape (attempt 3b5d7488...: 15 provider process
    attempts, 38,534 observed public-evidence bytes, patch applied, Validate
    visited, verifier never ran, interrupted controller outcome; the original
    run materialized no case record for it and aborted BUDGET_EXCEEDED) now
    flows end to end: outcome validation, budget rewriting, materialization,
    frozen result validation, campaign persistence and package verification.
    The case is resolved from its exact frozen case_id (order_index 2,
    find-in-sorted / static-baseline) and the persisted record proves the
    exact identity, preserves the interruption identity and every accounting
    counter, clamps public evidence to 20,000, and carries the machine-
    readable budget_exhaustion provenance; the campaign, which first
    completed the recorded find-in-sorted / pdb-on-uncertainty Case 1, then
    terminalizes as the typed ABORTED / INTERRUPTED package."""
    attempt = _fixture_attempt()
    case2 = attempt["case2_observed"]
    case = _resolve_fixture_case(manifest_v4, case2)
    assert case2["case_id"] == manifest_v4["case_order"][1]["case_id"]
    assert case2["policy"] == "static-baseline"
    raw = _fixture_outcome(manifest_v4, case, case2, interrupted=True, patch_applied=True)

    runner.validate_case_outcome(raw)
    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(raw, manifest_v4, case_policy=case["policy"])
    assert info.value.observed == 38534
    assert info.value.limit == 20000

    rewritten = runner._budget_exhausted_outcome(case, raw, info.value, run_id="replay-3b5d7488", manifest=manifest_v4)
    assert rewritten is not None
    assert rewritten["terminal_status"] == "INFRASTRUCTURE_ERROR"
    assert rewritten["terminal_reason_code"] == "INFRASTRUCTURE_FAILURE"
    assert rewritten["public_evidence_bytes"] == 20000
    assert rewritten["interrupted"] is True
    assert rewritten["patch_submissions"] == 1
    assert rewritten["candidate_provenance"] == "applied_patch_event"
    assert rewritten["verifier_runs"] == 0
    assert rewritten["logical_model_calls"] == 14
    assert rewritten["provider_process_attempts"] == 15
    assert rewritten["retries"] == 1
    assert rewritten["valid_directives"] == 14
    assert rewritten["controller_states_visited"] == ["Reproduce", "Understand", "Patch", "Validate", "Failed"]
    assert rewritten["budget_exhaustion"] == {
        "configured_limit": 20000, "observed_bytes": 38534, "persisted_bytes": 20000,
        "state": "exhausted", "truncated": True,
    }
    runner.validate_budget_exhaustion_provenance(rewritten["budget_exhaustion"], budgets=manifest_v4["budgets"])
    runner.enforce_case_budgets(rewritten, manifest_v4, case_policy=case["policy"])

    record = _materialize_and_validate(manifest_v4, case, rewritten, auth=auth_v4)
    assert record["case_id"] == case2["case_id"]
    assert record["order_index"] == case2["order_index"]
    assert record["task_id"] == case2["task_id"]
    assert record["policy"] == case2["policy"]
    assert record["public_evidence_bytes"] == 20000
    assert record["interrupted"] is True
    assert record["patch_submissions"] == 1
    assert record["verifier_runs"] == 0
    assert record["provider_process_attempts"] == 15
    assert record["budget_exhaustion"]["observed_bytes"] == 38534
    assert record["budget_exhaustion"]["persisted_bytes"] == 20000
    runner.validate_record_budget_exhaustion(record, budgets=manifest_v4["budgets"])

    entries = _completed_entries(manifest_v4)
    case1 = attempt["case1_observed"]
    entries[0] = {
        "provider_process_attempts": 10,
        "outcome": _fixture_outcome(
            manifest_v4, _resolve_fixture_case(manifest_v4, case1), case1,
            interrupted=False, patch_applied=False,
        ),
    }
    entries[1] = {"provider_process_attempts": 15, "outcome": raw}
    campaign_record, factory, case_runner, output = _run_campaign_custom(
        manifest_v4, auth_v4, tmp_path,
        case_runner=ScriptedCaseRunner(entries),
        runner_entries=entries,
        git_state_provider=git_state_provider,
    )
    assert campaign_record["status"] == "ABORTED"
    assert campaign_record["stop_reason"] == "INTERRUPTED"
    assert campaign_record["case_lifecycle_states"][case["case_id"]] == "completed"
    assert campaign_record["case_lifecycle_states"][manifest_v4["case_order"][0]["case_id"]] == "completed"
    first = campaign_record["cases"][1]
    assert first["case_id"] == case2["case_id"]
    assert first["order_index"] == case2["order_index"]
    assert first["task_id"] == case2["task_id"]
    assert first["policy"] == case2["policy"]
    assert first["terminal_status"] == "INFRASTRUCTURE_ERROR"
    assert first["public_evidence_bytes"] == 20000
    assert first["interrupted"] is True
    assert first["patch_submissions"] == 1
    assert first["verifier_runs"] == 0
    assert first["provider_process_attempts"] == 15
    assert first["logical_model_calls"] == 14
    assert first["budget_exhaustion"]["observed_bytes"] == 38534
    assert first["budget_exhaustion"]["persisted_bytes"] == 20000
    assert first["budget_exhaustion"]["state"] == "exhausted"
    runner.validate_campaign_record(campaign_record, manifest_v4)
    verification = runner.verify_attempt_package(output, manifest_v4)
    assert verification["consistent"] is True
    assert verification["case_files_on_disk"] == 2
    assert verification["case_records_referenced"] == 2
    assert verification["terminal_commit"] == "PRESENT"


def test_v4_attempt_case1_malformed_patch_replay_from_fixture(manifest_v4, auth_v4, tmp_path, git_state_provider):
    """The actual V4 Case 1 shape (malformed patch rejection, 26,139 observed
    public-evidence bytes, 10 provider processes, no applied candidate,
    verifier never ran) materializes as a schema-valid terminal with the
    budget clamped and the campaign continuing to the remaining cases.  The
    case is resolved from its exact frozen case_id (order_index 1,
    find-in-sorted / pdb-on-uncertainty), distinct from Case 2."""
    attempt = _fixture_attempt()
    case1 = attempt["case1_observed"]
    case = _resolve_fixture_case(manifest_v4, case1)
    assert case1["case_id"] != attempt["case2_observed"]["case_id"]
    assert case1["policy"] == "pdb-on-uncertainty"
    raw = _fixture_outcome(manifest_v4, case, case1, interrupted=False, patch_applied=False)
    assert raw["patch_submissions"] == 0
    assert raw["verifier_runs"] == 0

    runner.validate_case_outcome(raw)
    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(raw, manifest_v4, case_policy=case["policy"])
    assert info.value.observed == 26139

    rewritten = runner._budget_exhausted_outcome(case, raw, info.value, run_id="replay-3b5d7488-case1", manifest=manifest_v4)
    assert rewritten is not None
    assert rewritten["terminal_status"] == "INFRASTRUCTURE_ERROR"
    assert rewritten["public_evidence_bytes"] == 20000
    assert rewritten["interrupted"] is False
    assert rewritten["patch_submissions"] == 0
    assert rewritten["verifier_runs"] == 0
    assert rewritten["candidate_hash"] is None
    assert rewritten["budget_exhaustion"] == {
        "configured_limit": 20000, "observed_bytes": 26139, "persisted_bytes": 20000,
        "state": "exhausted", "truncated": True,
    }
    runner.validate_budget_exhaustion_provenance(rewritten["budget_exhaustion"], budgets=manifest_v4["budgets"])
    runner.enforce_case_budgets(rewritten, manifest_v4, case_policy=case["policy"])

    record = _materialize_and_validate(manifest_v4, case, rewritten, auth=auth_v4)
    assert record["case_id"] == case1["case_id"]
    assert record["order_index"] == case1["order_index"]
    assert record["task_id"] == case1["task_id"]
    assert record["policy"] == case1["policy"]
    assert record["public_evidence_bytes"] == 20000
    assert record["interrupted"] is False
    assert record["patch_submissions"] == 0
    assert record["verifier_runs"] == 0
    assert record["budget_exhaustion"]["observed_bytes"] == 26139

    entries = _completed_entries(manifest_v4)
    entries[0] = {"provider_process_attempts": 10, "outcome": raw}
    campaign_record, factory, case_runner, output = _run_campaign_custom(
        manifest_v4, auth_v4, tmp_path,
        case_runner=ScriptedCaseRunner(entries),
        runner_entries=entries,
        git_state_provider=git_state_provider,
    )
    assert campaign_record["status"] == "COMPLETED"
    case1_record = campaign_record["cases"][0]
    assert case1_record["case_id"] == case1["case_id"]
    assert case1_record["order_index"] == case1["order_index"]
    assert case1_record["policy"] == case1["policy"]
    assert case1_record["terminal_status"] == "INFRASTRUCTURE_ERROR"
    assert case1_record["public_evidence_bytes"] == 20000
    assert case1_record["interrupted"] is False
    assert case1_record["patch_submissions"] == 0
    assert case1_record["verifier_runs"] == 0
    assert case1_record["budget_exhaustion"]["observed_bytes"] == 26139
    assert runner.verify_attempt_package(output, manifest_v4)["consistent"] is True
    runner.validate_campaign_record(campaign_record, manifest_v4)


def test_budget_exhaustion_provenance_is_fail_closed(manifest_v4):
    """The machine-readable provenance is validated fail-closed: wrong field
    sets, contradictory counts, and a persisted byte count above the frozen
    limit are rejected."""
    case = manifest_v4["case_order"][0]
    budgets = manifest_v4["budgets"]
    valid = {
        "configured_limit": 20000, "observed_bytes": 38534, "persisted_bytes": 20000,
        "state": "exhausted", "truncated": True,
    }
    runner.validate_budget_exhaustion_provenance(valid, budgets=budgets)
    with pytest.raises(runner.LiveRunnerError):
        runner.validate_budget_exhaustion_provenance(dict(valid, configured_limit=19999), budgets=budgets)
    with pytest.raises(runner.LiveRunnerError):
        runner.validate_budget_exhaustion_provenance(dict(valid, observed_bytes=20000), budgets=budgets)
    with pytest.raises(runner.LiveRunnerError):
        runner.validate_budget_exhaustion_provenance(dict(valid, persisted_bytes=20001), budgets=budgets)
    with pytest.raises(runner.LiveRunnerError):
        runner.validate_budget_exhaustion_provenance(dict(valid, state="truncated"), budgets=budgets)
    with pytest.raises(runner.LiveRunnerError):
        runner.validate_budget_exhaustion_provenance(dict(valid, truncated=False), budgets=budgets)
    with pytest.raises(runner.LiveRunnerError):
        runner.validate_budget_exhaustion_provenance({k: v for k, v in valid.items() if k != "state"}, budgets=budgets)
    with pytest.raises(runner.LiveRunnerError):
        runner.validate_budget_exhaustion_provenance(dict(valid, extra_field=1), budgets=budgets)


def test_adapter_maps_reporting_interrupted_to_outcome_interrupted(manifest_v4):
    """Adapter-level contract: ``reporting.interrupted`` from a real
    LiveCaseResult mapping propagates to ``outcome.interrupted`` (True and
    False) through the accepted outcome mapping."""
    case = manifest_v4["case_order"][1]
    source_hash = next(item["source_sha256"] for item in manifest_v4["inventory"] if item["task_id"] == case["task_id"])

    def build_live_result(interrupted: bool) -> LiveCaseResult:
        base = _live_mapping(manifest_v4, case, **{
            "status": "INCOMPLETE" if interrupted else "UNRESOLVED",
            "controller": {"completed": False, "final_state": None, "stop_reason": "interrupted" if interrupted else None,
                           "model_calls": 0, "exception": False},
            "verifier": {"executed": False, "failure": False, "status": None, "outcome": None,
                         "patch_application": None, "localization": {"outcome": "NO_LOCALIZATION"}},
            "measurements": {
                "model_request_count": 0, "model_response_count": 0, "retry_count": 0,
                "provider_error_count": 0, "provider_error_kinds": [],
                "token_usage": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
                                "provider_reported": False, "missing_fields": []},
                "termination_reason": "interrupted" if interrupted else None,
                "successful_pdb_observation_count": 0, "failed_pdb_observation_count": 0,
                "tool_call_count": 0, "case_elapsed_duration_ms": 0,
                "model_phase_elapsed_duration_ms": 0, "model_transport_duration_ms": 0,
                "elapsed_scope": "case_observed; model_phase=transport_only",
            },
            "reporting": {"mode": "live", "completed": not interrupted, "partial": interrupted,
                          "interrupted": interrupted, "event_recorded": False, "cleanup": "cleaned",
                          "case_directory_owned": True},
            "events_jsonl": "",
            "evidence": {"pdb_gate_decisions": [], "directive_rejections": []},
        })
        return LiveCaseResult(
            task_id=base["task_id"], policy=base["policy"], repetition=base["repetition"],
            status=LiveCaseStatus.INCOMPLETE if interrupted else LiveCaseStatus.UNRESOLVED,
            controller=base["controller"], verifier=base["verifier"], measurements=base["measurements"],
            reporting=base["reporting"], events_jsonl=base["events_jsonl"], diagnostics=(),
            case_id=base["case_id"], run_id=base["run_id"], trajectory_id=base["trajectory_id"],
            evidence=base["evidence"],
        )

    interrupted_result = build_live_result(interrupted=True)
    assert interrupted_result.to_mapping()["reporting"]["interrupted"] is True
    inner = _InnerTransport("v4-interrupted-flag", process_attempts=0)
    outcome_interrupted = adapter._outcome_from_live_case(
        case, interrupted_result, _route_observation(manifest_v4), inner,
        transport_attempts=0, policy_value="static-baseline", run_id="v4-interrupted-flag",
        source_hash=source_hash,
    )
    assert outcome_interrupted["interrupted"] is True

    normal_result = build_live_result(interrupted=False)
    assert normal_result.to_mapping()["reporting"]["interrupted"] is False
    inner.process_attempts = 1
    outcome_normal = adapter._outcome_from_live_case(
        case, normal_result, _route_observation(manifest_v4), inner,
        transport_attempts=1, policy_value="static-baseline", run_id="v4-interrupted-flag-normal",
        source_hash=source_hash,
    )
    assert outcome_normal["interrupted"] is False


# ---- fail-closed budget provenance after persistence --------------------------


def _v4_case2_campaign_record(manifest_v4, auth_v4, tmp_path, git_state_provider):
    """Build the actual V4 Case 2 campaign (ABORTED / INTERRUPTED with the
    interrupted find-in-sorted / static-baseline budget-exhausted case record
    at frozen order 2) and return (record, output_root)."""
    attempt = _fixture_attempt()
    case2 = attempt["case2_observed"]
    case = _resolve_fixture_case(manifest_v4, case2)
    raw = _fixture_outcome(manifest_v4, case, case2, interrupted=True, patch_applied=True)
    entries = _completed_entries(manifest_v4)
    entries[1] = {"provider_process_attempts": 15, "outcome": raw}
    record, factory, case_runner, output = _run_campaign_custom(
        manifest_v4, auth_v4, tmp_path,
        case_runner=ScriptedCaseRunner(entries),
        runner_entries=entries,
        git_state_provider=git_state_provider,
    )
    assert record["status"] == "ABORTED"
    assert record["cases"][1]["budget_exhaustion"]["persisted_bytes"] == 20000
    return record, output


def _recompute_record_sha256(entry: dict) -> dict:
    body = {key: value for key, value in entry.items() if key != "record_sha256"}
    entry["record_sha256"] = pilot.result_sha256(body)
    return entry


def test_campaign_validation_rejects_bad_budget_provenance(manifest_v4, auth_v4, tmp_path, git_state_provider):
    """validate_campaign_record rejects every recorded provenance corruption:
    persisted bytes differing from public_evidence_bytes, wrong configured
    limit, observed bytes not exceeding the limit, contradictory truncated
    state, and malformed/extra provenance fields.  Hash bindings alone never
    substitute for the semantic check: the embedded record_sha256 is
    recomputed so the provenance check is the rejecting gate."""
    record, output = _v4_case2_campaign_record(manifest_v4, auth_v4, tmp_path, git_state_provider)
    case_entry = copy.deepcopy(record["cases"][1])

    def expect_reject(mutate, message_part):
        mutated = copy.deepcopy(case_entry)
        mutate(mutated)
        mutated = _recompute_record_sha256(mutated)
        cases = [copy.deepcopy(record["cases"][0]), mutated]
        with pytest.raises(runner.LiveRunnerError, match=message_part):
            runner.validate_campaign_record({**record, "cases": cases}, manifest_v4)

    expect_reject(
        lambda entry: entry["budget_exhaustion"].update(persisted_bytes=19999),
        "persisted bytes \\(19999\\) differ from the persisted public evidence bytes \\(20000\\)",
    )
    expect_reject(
        lambda entry: entry["budget_exhaustion"].update(configured_limit=19999),
        "configured limit does not match the frozen budget",
    )
    expect_reject(
        lambda entry: entry["budget_exhaustion"].update(observed_bytes=20000, truncated=False),
        "observed bytes do not exceed the configured limit",
    )
    expect_reject(
        lambda entry: entry["budget_exhaustion"].update(truncated=False),
        "truncated flag is inconsistent",
    )
    expect_reject(
        lambda entry: entry["budget_exhaustion"].update(extra_field=1),
        "fields are not exact",
    )
    expect_reject(
        lambda entry: entry["budget_exhaustion"].pop("state"),
        "fields are not exact",
    )
    # The unmutated record still validates.
    runner.validate_campaign_record(record, manifest_v4)


def test_package_verification_rejects_bad_budget_provenance(manifest_v4, auth_v4, tmp_path, git_state_provider):
    """verify_attempt_package rejects a package whose persisted case record
    carries corrupted budget-exhaustion provenance (through campaign
    validation), even when every hash binding is recomputed consistently."""
    record, output = _v4_case2_campaign_record(manifest_v4, auth_v4, tmp_path, git_state_provider)
    assert runner.verify_attempt_package(output, manifest_v4)["consistent"] is True

    def tamper(provenance_mutate):
        root = Path(output)
        campaign = json.loads((root / "campaign.json").read_text(encoding="utf-8"))
        commit = json.loads((root / "terminal-commit.json").read_text(encoding="utf-8"))
        entry = campaign["cases"][1]
        case_path = root / "cases" / f"case-{int(entry['order_index']):02d}-{entry['case_id'].replace(':', '__')}.json"
        on_disk = json.loads(case_path.read_text(encoding="utf-8"))
        provenance_mutate(on_disk["budget_exhaustion"])
        new_sha = pilot.result_sha256(on_disk)
        entry["record_sha256"] = new_sha
        payload = lambda value: json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
        case_path.write_text(payload(on_disk), encoding="utf-8")
        (root / "campaign.json").write_text(payload(campaign), encoding="utf-8")
        commit["campaign_json_sha256"] = hashlib.sha256((root / "campaign.json").read_bytes()).hexdigest()
        commit["case_inventory"] = [
            {"case_id": item["case_id"], "order_index": item["order_index"], "record_sha256": item["record_sha256"]}
            for item in campaign["cases"]
        ]
        (root / "terminal-commit.json").write_text(payload(commit), encoding="utf-8")

    tamper(lambda provenance: provenance.update(persisted_bytes=19999))
    with pytest.raises(runner.LiveRunnerError, match="campaign record consistency"):
        runner.verify_attempt_package(output, manifest_v4)

    tamper(lambda provenance: provenance.update(configured_limit=19999))
    with pytest.raises(runner.LiveRunnerError, match="campaign record consistency"):
        runner.verify_attempt_package(output, manifest_v4)
