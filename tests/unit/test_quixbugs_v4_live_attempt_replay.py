"""Deterministic local replay of the preserved paired-pilot v3 attempt evidence.

Replays the sanitized fixture
``tests/fixtures/quixbugs_v4_replay_fixture.json`` (derived from the preserved
private evidence of attempt
``quixbugs-paired-pilot-v3-attempt-fddf1e39b73cda5f430d8e69c6e442b558143a63d013229e54efd9cbb585fbac``,
public protocol material and aggregate accounting numbers only) through the
exact extraction, validation, retry-accounting, and v4 terminalization paths:

* attempt 1: the preserved assistant directive text extracts through the
  schema-aware wrapper extraction and parses into an accepted
  ``run_reproduction(baseline)`` action directive, and the controller accepts
  and dispatches it (baseline reproduction observation);
* attempt 10: the preserved ``no_text_event`` provider shape (step_start +
  step_finish events, zero assistant text) classifies as ``no_text_event``;
* retry accounting: the preserved 13 process attempts / 12 completed
  responses / 1 exit failure map to 12 logical calls + 1 bounded retry, and
  the retry stays inside the frozen per-call transport-retry budget;
* the exact observed completed post-apply budget-exhaustion shape
  (33,685 public evidence bytes) materializes under the v4 contract with all
  accounting preserved, and still aborts under the frozen v3 contract.

No provider is contacted; the fixture carries no credentials, raw provider
output, usage, cost, or private evidence.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

import quixbugs_live_runner_v2 as runner
import quixbugs_paired_pilot as pilot
from scripts import opencode_protocol_transport as transport

from agentic_debugger.agent.controller import ControllerRunConfig, DeterministicController
from agentic_debugger.agent.controller_policy import (
    ActionName,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
)
from agentic_debugger.agent.model_adapter import ControllerSnapshot
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import ToolRegistry, ToolResult, ToolSpec
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.evaluation.live import (
    LiveModelAdapter,
    LiveModelConfig,
    LiveRunLimits,
    LiveTransportError,
    _parse,
)
from agentic_debugger.events.schema import ObservationStatus

from test_quixbugs_case_budget_terminal import (
    _completed_post_apply_exhausted_outcome,
    _curated_task,
    _route_evidence,
)

FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "quixbugs_v4_replay_fixture.json"


@pytest.fixture(scope="module")
def replay_fixture() -> dict:
    with open(FIXTURE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def manifest_v4():
    return pilot.load_manifest(pilot.MANIFEST_PATH_V4)


def _attempt1_registry() -> ToolRegistry:
    return ToolRegistry((
        ToolSpec(
            ActionName.RUN_REPRODUCTION,
            lambda arguments: dict(arguments),
            lambda _action, _arguments: ToolResult(
                ObservationStatus.OK,
                {"phase": "baseline", "failure_reproduced": True, "exit_code": 1, "expected_exit_code": 1},
                "ok",
            ),
            argument_contract={
                "required": ["phase"],
                "properties": {"phase": {"type": "string", "min_length": 1}},
                "additional_properties": False,
            },
        ),
    ))


class _ReplayTransport:
    """Scripted transport replaying preserved provider responses verbatim."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.index = 0
        self.payloads = []

    def request(self, payload, timeout_seconds):
        self.payloads.append(payload)
        response = self.responses[min(self.index, len(self.responses) - 1)]
        self.index += 1
        if isinstance(response, Exception):
            raise response
        return response


def test_replay_attempt1_directive_extracts_parses_and_dispatches(replay_fixture):
    """Attempt 1 was contract-valid: the preserved assistant text extracts to
    exactly one schema-valid directive, parses into the accepted
    run_reproduction(baseline) action, and dispatches to a baseline
    reproduction observation."""
    request = replay_fixture["attempt1_request"]
    text = replay_fixture["attempt1_extracted_text"]

    directive = transport._extract_directive(text, request)
    assert directive == {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}

    request_byte_count = len(transport.canonical_public_request(request).encode("utf-8"))
    assert request_byte_count == 4861
    assert request_byte_count <= transport.MAX_PUBLIC_EVIDENCE_BYTES

    task = _curated_task()
    registry = _attempt1_registry()
    snapshot = ControllerSnapshot(
        "replay-run", task.task_id, ControllerState.REPRODUCE, 0,
        ControllerBudgetLimits.from_task_constraints(task.constraints),
        ControllerBudgetState(), HypothesisLedger(),
    )
    contracts = transport._request_action_contracts(request)
    schema = transport._request_directive_schema(request)
    legal = set(transport._request_controller(request).get("legal_transition_targets", []))
    parsed = _parse(
        directive, snapshot,
        action_contracts=contracts,
        directive_kinds=set(schema),
        legal_transition_targets=legal,
    )
    assert parsed.kind.value == "action"
    assert parsed.name is ActionName.RUN_REPRODUCTION
    assert parsed.arguments == {"phase": "baseline"}

    live_adapter = LiveModelAdapter(
        task=task, policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        config=LiveModelConfig("replay-model", ("replay-command",)),
        transport=_ReplayTransport([{"directive": directive, "usage": {"prompt_tokens": 2145, "completion_tokens": 20, "total_tokens": 4380}}]),
        limits=LiveRunLimits(max_model_requests=1, max_controller_steps=1),
        registry=registry, evaluation_id="replay", case_id="replay-case", run_id="replay-run", trajectory_id="replay-run",
    )
    controller = DeterministicController(registry, live_adapter, ControllerRunConfig(max_model_calls=1))
    result = controller.run(snapshot)
    assert live_adapter.metrics.model_requests == 1
    assert live_adapter.metrics.model_responses == 1
    reproduced = [
        step for step in result.steps
        if step.action is not None and step.action.name == ActionName.RUN_REPRODUCTION.value
        and step.observation is not None and step.observation.payload.get("failure_reproduced") is True
    ]
    assert reproduced, "the accepted attempt-1 directive was not dispatched to a baseline reproduction"


def test_replay_attempt10_no_text_event_classification(replay_fixture):
    """The preserved attempt-10 provider shape (step_start + step_finish
    events, zero assistant text) classifies as no_text_event and carries no
    extractable directive text."""
    events = [
        {"type": "step_start", "timestamp": 1, "sessionID": "s", "part": {"type": "step-start"}},
        {"type": "step_finish", "timestamp": 2, "sessionID": "s", "part": {"type": "step-finish"}},
    ]
    assert replay_fixture["attempt10_event_type_counts"] == {"step_finish": 1, "step_start": 1}
    assert replay_fixture["attempt10_extracted_text_part_count"] == 0
    text_parts = []
    classification = transport._parse_failure_classification(
        "opencode stream events without assistant text\n", events, text_parts, [],
    )
    assert classification == "no_text_event"
    assert not text_parts


def test_replay_bounded_retry_accounting(replay_fixture):
    """The preserved 13 process attempts / 12 completed responses / 1 exit
    failure map to 12 logical calls plus one bounded retry that stays inside
    the frozen per-call transport-retry budget; the retried logical call
    accounts as 2 attempts, 1 response, 1 retry."""
    observed = replay_fixture["observed"]
    assert observed["provider_process_attempts"] == 13
    assert observed["completed_provider_responses"] == 12
    assert observed["provider_exit_failures"] == 1
    assert observed["logical_model_calls"] == 12
    assert observed["retries"] == 1
    assert observed["provider_process_attempts"] == observed["logical_model_calls"] + observed["retries"]
    assert observed["retries"] <= observed["logical_model_calls"] * observed["per_call_transport_retry_limit"]
    assert observed["provider_process_attempts"] <= observed["logical_model_calls"] * 3

    task = _curated_task()
    registry = _attempt1_registry()
    directive = {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}
    live_adapter = LiveModelAdapter(
        task=task, policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        config=LiveModelConfig("replay-model", ("replay-command",)),
        transport=_ReplayTransport([
            LiveTransportError("provider process exited nonzero", kind="process_error"),
            {"directive": directive, "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        ]),
        limits=LiveRunLimits(max_model_requests=2, max_retries=2, max_controller_steps=2),
        registry=registry, evaluation_id="replay", case_id="replay-case", run_id="replay-run", trajectory_id="replay-run",
    )
    controller = DeterministicController(registry, live_adapter, ControllerRunConfig(max_model_calls=2))
    controller.run(ControllerSnapshot(
        "replay-run", task.task_id, ControllerState.REPRODUCE, 0,
        ControllerBudgetLimits.from_task_constraints(task.constraints),
        ControllerBudgetState(), HypothesisLedger(),
    ))
    metrics = live_adapter.metrics
    assert metrics.model_requests == 2
    assert metrics.model_responses == 1
    assert metrics.retries == 1
    assert metrics.provider_errors == 1
    assert metrics.provider_error_kinds == ["process_error"]
    assert metrics.model_requests == metrics.model_responses + metrics.retries


def test_replay_request_byte_growth_within_budget(replay_fixture):
    """Every preserved canonical public request stayed inside the frozen
    20,000-byte per-request budget; the attempt-1 request replays to exactly
    its recorded byte count through the canonical serializer."""
    counts = {item["attempt"]: item["request_byte_count"] for item in replay_fixture["attempt_request_byte_counts"]}
    assert counts[1] == 4861
    assert len(counts) == 13
    assert all(count <= 20000 for count in counts.values())
    assert max(counts.values()) == 19605

    request = replay_fixture["attempt1_request"]
    assert len(transport.canonical_public_request(request).encode("utf-8")) == 4861


def test_replay_observed_shape_terminalizes_under_v4(replay_fixture, manifest_v4):
    """The exact observed completed post-apply budget-exhaustion shape
    materializes under the v4 contract with all accounting preserved and the
    exact 33,685 byte count in the termination detail."""
    observed = replay_fixture["observed"]
    case = manifest_v4["case_order"][0]
    route = _route_evidence(manifest_v4)
    outcome = _completed_post_apply_exhausted_outcome(manifest_v4, case, route, **{
        "public_evidence_bytes": observed["public_evidence_bytes"],
        "logical_model_calls": observed["logical_model_calls"],
        "provider_process_attempts": observed["provider_process_attempts"],
        "retries": observed["retries"],
        "valid_directives": observed["valid_directives"],
    })

    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(outcome, manifest_v4, case_policy=case["policy"])
    assert info.value.observed == 33685
    assert info.value.limit == 20000

    rewritten = runner._budget_exhausted_outcome(
        case, outcome, info.value, run_id="replay-fddf1e39", manifest=manifest_v4,
    )
    assert rewritten is not None
    assert rewritten["terminal_status"] == "RESOLVED"
    assert rewritten["public_evidence_bytes"] == 20000
    assert "33685" in rewritten["termination_reason"]
    assert rewritten["logical_model_calls"] == 12
    assert rewritten["provider_process_attempts"] == 13
    assert rewritten["retries"] == 1
    assert rewritten["valid_directives"] == 12
    assert rewritten["patch_submissions"] == 1
    assert rewritten["candidate_provenance"] == "verifier_record"
    assert rewritten["verifier_runs"] == 1
    assert rewritten["pdb_counts"] == dict(runner.ZERO_PDB_COUNTS)


def test_replay_v3_aborts_on_observed_shape(replay_fixture):
    """The same raw shape under the frozen v3 contract returns None (the
    observed honest abort): the frozen v3 terminal matrix has no
    representation for provider contact, an applied candidate, an executed
    verifier, and public-evidence exhaustion."""
    manifest_v3 = pilot.load_manifest(pilot.MANIFEST_PATH_V3)
    case = manifest_v3["case_order"][0]
    route = _route_evidence(manifest_v3)
    outcome = _completed_post_apply_exhausted_outcome(manifest_v3, case, route, **{
        "terminal_status": "PDB_NOT_REACHED",
        "terminal_reason_code": "PDB_NOT_REACHED_NO_GATE",
        "termination_reason": "opencode-go adapter: PDB_NOT_REACHED: completed",
        "repair_outcome": "NO_CANDIDATE",
    })
    with pytest.raises(runner.PublicEvidenceBudgetExhausted) as info:
        runner.enforce_case_budgets(outcome, manifest_v3, case_policy=case["policy"])
    assert runner._budget_exhausted_outcome(case, outcome, info.value, run_id="replay-v3", manifest=manifest_v3) is None


def test_replay_fixture_is_sanitized(replay_fixture):
    """The fixture carries no credentials, auth material, raw provider
    output, usage, cost, or private evidence; only public protocol material
    and aggregate numbers."""
    blob = json.dumps(replay_fixture, ensure_ascii=False)
    for marker in ("auth.json", "api_key", "bearer", "sessionID", '"type": "step_start"', '"part"', "provider_stdout", "provider_stderr", "usage", "cost"):
        assert marker not in blob, f"private marker leaked: {marker}"
    assert set(replay_fixture) == {
        "attempt10_event_type_counts",
        "attempt10_extracted_text_part_count",
        "attempt1_extracted_text",
        "attempt1_request",
        "attempt_request_byte_counts",
        "observed",
    }
