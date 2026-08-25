"""Frozen-contract tests for the UI-driven lower capability rungs."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_debugger.application import ollama_cloud_source as source
from agentic_debugger.application.worker_scenarios import ScenarioContext
from agentic_debugger.cancellation import CancellationToken
from agentic_debugger.agent.controller_policy import (
    ActionName,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
)
from agentic_debugger.agent.model_adapter import ActionDirective, ControllerSnapshot
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.demo.catalog import scenario_for
from agentic_debugger.demo.policies import DemoPolicy, PdbPolicy
from agentic_debugger.demo.tools import DemoToolContext, build_registry
from agentic_debugger.evaluation.live import LiveModelAdapter, LiveModelConfig, LiveRunLimits
from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.runtime.workspace import TaskWorkspace


ROOT = Path(__file__).resolve().parents[2]
LOWER_RUNG_IDS = (
    "pdb-required-boundary-006",
    "pdb-required-caller-callee-007",
    "pdb-required-multistage-units-008",
)


@pytest.mark.parametrize(
    ("task_id", "phase_seconds"),
    [
        ("pdb-required-boundary-006", 600),
        ("pdb-required-caller-callee-007", 3600),
        ("pdb-required-multistage-units-008", 3600),
    ],
)
def test_lower_rung_contracts_match_accepted_treatments(task_id: str, phase_seconds: int) -> None:
    contract = source.ladder_runtime_contract(task_id)
    assert (contract.max_model_requests, contract.max_controller_steps) == (24, 24)
    assert contract.max_model_phase_seconds == phase_seconds
    assert contract.max_retries == 0


def test_ladder_command_exactly_matches_proven_level32_adapter_path() -> None:
    from scripts.run_cookiecutter_967_pdb_proof import _adapter_config

    alias = "deepseek-v4-flash:cloud"
    ladder, profile = source._config(alias, logical_call_ceiling=24)
    proven = _adapter_config(ROOT, model=alias, logical_decision_ceiling=24)
    assert profile.local_alias == alias
    assert ladder.model_name == alias
    assert ladder.command == proven.command
    assert ladder.request_timeout_seconds == proven.request_timeout_seconds


def test_level6_first_request_reaches_canonical_adapter_contract_and_parses_success(
    tmp_path: Path,
) -> None:
    from scripts.ollama_cloud_command_adapter import canonical_public_request

    task_id = "pdb-required-boundary-006"
    fixture = ROOT / "agentic_debugger" / "datasets" / "curated" / task_id
    workspace = TaskWorkspace(str(fixture), parent_dir=str(tmp_path))
    task = load_task(str(fixture / "task.json"))
    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=None)
    registry = build_registry(
        context,
        pdb_policy=PdbPolicy.ON_UNCERTAINTY,
        interactive_debugger_controls=True,
    )
    captured: list[dict[str, object]] = []

    class FakeSuccessfulTransport:
        def request(self, payload, timeout_seconds):
            del timeout_seconds
            captured.append(dict(payload))
            return {
                "provider_completion_schema_version": "provider-completion-v1",
                "directive_content": (
                    '{"kind":"action","name":"run_reproduction",'
                    '"arguments":{"phase":"baseline"}}'
                ),
            }

    config, _profile = source._config(
        "deepseek-v4-flash:cloud", logical_call_ceiling=24
    )
    adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.PDB_ON_UNCERTAINTY,
        config=config,
        transport=FakeSuccessfulTransport(),
        limits=LiveRunLimits(
            max_model_requests=24,
            max_controller_steps=24,
            max_model_phase_seconds=600,
            max_retries=0,
            continue_on_task_failure=False,
        ),
        registry=registry,
        proof_required=True,
        proof_source_line=scenario_for(task_id).runtime_probe.breakpoint_line,
        proof_observed_local_names=scenario_for(task_id).runtime_probe.inspect_expressions,
    )
    try:
        directive = adapter.next_directive(
            ControllerSnapshot(
                "run-level6-fake-success",
                task_id,
                ControllerState.REPRODUCE,
                0,
                ControllerBudgetLimits.from_task_constraints(task.constraints),
                ControllerBudgetState(),
                HypothesisLedger(),
            )
        )
        assert directive == ActionDirective(
            ActionName.RUN_REPRODUCTION, {"phase": "baseline"}
        )
        assert len(captured) == 1
        canonical = canonical_public_request(captured[0])
        assert len(canonical.encode("utf-8")) <= 32_768
        assert captured[0]["protocol"]["logical_model_call_index"] == 0
    finally:
        workspace.cleanup()


@pytest.mark.parametrize("task_id", LOWER_RUNG_IDS)
def test_ui_ladder_source_binds_exact_probe_and_all_frozen_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task_id: str
) -> None:
    captured: dict[str, object] = {}
    scenario = scenario_for(task_id)
    profile = source._config(
        "gpt-oss:20b-cloud", logical_call_ceiling=24
    )[1]

    class FakeEmitter:
        session_id = "sess-ui-ladder-contract"
        source_kind = None

        def __init__(self, task_id: str):
            self.task_id = task_id

        def emit(self, kind, payload):
            del kind, payload

    class FakeAdapter:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    def fake_run_local_session(ctx, **kwargs):
        captured["run_local_session"] = kwargs
        kwargs["model_factory"](SimpleNamespace(task=object()), object())

    monkeypatch.setattr(source, "LiveModelAdapter", FakeAdapter)
    monkeypatch.setattr(source, "run_local_session", fake_run_local_session)
    ctx = ScenarioContext(
        work_dir=tmp_path,
        token=CancellationToken(),
        emitter=FakeEmitter(task_id),
        run_id="run-ui-ladder-contract",
    )

    source.run_ollama_cloud_session(
        ctx,
        {"model_alias": profile.local_alias, "policy": DemoPolicy.PDB_ON_UNCERTAINTY.value},
    )

    limits = captured["limits"]
    assert limits.max_model_requests == 24
    assert limits.max_controller_steps == 24
    assert limits.max_model_phase_seconds == source.ladder_runtime_contract(task_id).max_model_phase_seconds
    assert limits.max_retries == 0
    assert captured["proof_required"] is True
    assert captured["proof_source_line"] == scenario.runtime_probe.breakpoint_line
    assert captured["proof_observed_local_names"] == scenario.runtime_probe.inspect_expressions
    assert captured["run_local_session"]["max_model_calls"] == 24
    command = captured["config"].command
    assert command[command.index("--max-logical-model-calls") + 1] == "24"


def test_exact_pdb_adapter_rejects_patch_evidence_without_runtime_proof(tmp_path: Path) -> None:
    task_id = "pdb-required-boundary-006"
    scenario = scenario_for(task_id)
    fixture = ROOT / "agentic_debugger" / "datasets" / "curated" / task_id
    workspace = TaskWorkspace(str(fixture), parent_dir=str(tmp_path))
    task = load_task(str(fixture / "task.json"))
    context = DemoToolContext(task=task, workspace=workspace, patch="", probe=None)
    registry = build_registry(
        context,
        pdb_policy=PdbPolicy.ON_UNCERTAINTY,
        interactive_debugger_controls=True,
    )
    try:
        adapter = LiveModelAdapter(
            task=task,
            policy=DemoPolicy.PDB_ON_UNCERTAINTY,
            config=LiveModelConfig("zero-provider", ("zero-provider",)),
            transport=SimpleNamespace(),
            limits=LiveRunLimits(max_model_requests=1, max_retries=0),
            registry=registry,
            proof_required=scenario.runtime_probe.exact_public_reproduction,
            proof_source_line=scenario.runtime_probe.breakpoint_line,
            proof_observed_local_names=scenario.runtime_probe.inspect_expressions,
        )
        assert adapter._proof_patch_allowed() is False
        assert adapter._proof_diagnosis_ready() is False
    finally:
        workspace.cleanup()
