"""Task-44 repair F3: generic omission means unbounded; explicit finite honored.

Regression matrix (behavioral, no real providers):

* generic Controller default (omitted ``max_model_calls``) → unbounded;
* generic LiveRunLimits default (omitted request/step dims) → unbounded;
* ModelGateway generic defaults (omitted ceilings) → unbounded;
* provider live-config generic default (omitted ceiling) → unbounded argv;
* builder omission (commandcode / opencode-provider / ollama) → ``0`` argv
  that accepts indices beyond historical defaults;
* run_adapter signature defaults → unbounded sentinel, accepted past old
  defaults;
* provider-adapter sentinel (``0``) accepts indices beyond every
  historical default on all six routes;
* representative CLI black-box: omitted ``--max-logical-model-calls``
  accepts index 200 (post-gate failure, never ``logical_call_limit``),
  while explicit ``--max-logical-model-calls 5`` still rejects it;
* explicit finite controller harness still terminates at N;
* explicit frozen/scientific budget still honors its treatment N.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agentic_debugger.agent.controller import (  # noqa: E402
    ControllerRunConfig,
    ControllerStopReason,
    DeterministicController,
)
from agentic_debugger.agent.controller_policy import (  # noqa: E402
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
)
from agentic_debugger.agent.model_adapter import (  # noqa: E402
    ControllerSnapshot,
    ScriptedModelAdapter,
    ScriptedModelStep,
    TransitionDirective,
)
from agentic_debugger.agent.state_machine import ControllerState  # noqa: E402
from agentic_debugger.agent.tool_registry import ToolRegistry  # noqa: E402
from agentic_debugger.demo.policies import DemoPolicy  # noqa: E402
from agentic_debugger.evaluation.live import (  # noqa: E402
    LiveModelAdapter,
    LiveModelConfig,
    LiveRunLimits,
    LiveTreatmentBudget,
)
from agentic_debugger.evaluation.runner import load_task  # noqa: E402

import commandcode_goat_adapter as commandcode_adapter  # noqa: E402
import opencode_provider_adapter as opencode_adapter  # noqa: E402
import provider_direct_api_adapter as direct_adapter  # noqa: E402
import ollama_cloud_command_adapter as ollama_adapter  # noqa: E402
import opencode_go_command_adapter as opencode_go_adapter  # noqa: E402
import agy_gemini_command_adapter as agy_adapter  # noqa: E402


def _valid_request_0based(index: int) -> dict:
    return {
        "protocol": {
            "name": "agentic-debugger-live-jsonl",
            "version": "1.3",
            "logical_model_call_index": index,
        }
    }


def _valid_request_1based(index: int) -> dict:
    return {"protocol": {"logical_model_call_index": index}}


def _argv_ceiling(command: tuple[str, ...]) -> str:
    flag = "--max-logical-model-calls"
    assert flag in command, f"ceiling flag missing: {command!r}"
    return command[command.index(flag) + 1]


# ---------------------------------------------------------------------------
# generic Controller default → unbounded (behavioral, 71 > 64/40/32/25/24)
# ---------------------------------------------------------------------------


def test_generic_controller_default_is_unbounded() -> None:
    assert ControllerRunConfig().max_model_calls is None
    steps = tuple(
        ScriptedModelStep(
            ControllerState.UNDERSTAND,
            TransitionDirective(ControllerState.UNDERSTAND, f"churn {i}"),
        )
        for i in range(70)
    ) + (
        ScriptedModelStep(
            ControllerState.UNDERSTAND,
            TransitionDirective(ControllerState.FAILED, "scripted end"),
        ),
    )
    adapter = ScriptedModelAdapter(steps)
    controller = DeterministicController(ToolRegistry(), adapter)  # default omitted
    result = controller.run(
        ControllerSnapshot(
            "run-dflt", "task-dflt", ControllerState.UNDERSTAND, 0,
            ControllerBudgetLimits(1, 1, 0), ControllerBudgetState(), HypothesisLedger(),
        )
    )
    assert result.stop_reason is ControllerStopReason.FAILED
    assert result.model_calls == 71
    assert len(result.steps) == 71


# ---------------------------------------------------------------------------
# generic LiveRunLimits default → unbounded (behavioral through the adapter)
# ---------------------------------------------------------------------------


def test_generic_live_limits_default_is_unbounded() -> None:
    limits = LiveRunLimits()
    assert limits.max_model_requests is None
    assert limits.max_controller_steps is None

    task = load_task(
        str(REPO_ROOT / "agentic_debugger" / "datasets" / "curated"
            / "curated-off-by-one-002" / "task.json")
    )
    directives = [
        {"kind": "transition", "target_state": "Understand", "reason": f"churn {i}"}
        for i in range(70)
    ]
    directives.append(
        {"kind": "transition", "target_state": "Failed", "reason": "scripted end"}
    )
    calls: list[dict] = []

    class FakeTransport:
        def request(self, payload, timeout_seconds):
            idx = len(calls)
            calls.append(payload)
            return {
                "directive": dict(directives[idx]),
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

    adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.STATIC_BASELINE,
        config=LiveModelConfig(model_name="m", command=("echo", "hi"),
                               request_timeout_seconds=30, tool_version="t"),
        transport=FakeTransport(),
        limits=limits,  # generic default
        registry=ToolRegistry(()),
        evaluation_id="e", case_id="c", run_id="r", trajectory_id="t",
    )
    controller = DeterministicController(ToolRegistry(), adapter)  # default omitted
    result = controller.run(
        ControllerSnapshot(
            "r", task.task_id, ControllerState.UNDERSTAND, 0,
            ControllerBudgetLimits.from_task_constraints(task.constraints),
            ControllerBudgetState(), HypothesisLedger(),
        )
    )
    assert result.stop_reason is ControllerStopReason.FAILED
    assert result.model_calls == 71
    assert adapter.metrics.model_requests == 71
    assert adapter.metrics.termination_reason is None


# ---------------------------------------------------------------------------
# gateway + provider live-config generic defaults → unbounded
# ---------------------------------------------------------------------------


def _isolate_loopback_provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from agentic_debugger.application import provider_connections as pc
    from agentic_debugger.application.provider_connections import (
        AUTH_NONE, PROTOCOL_CHAT_COMPLETIONS, TRANSPORT_GENERIC,
    )

    monkeypatch.setenv(
        "AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH",
        str(tmp_path / "provider-configurations.json"),
    )
    pc.add_provider_config(
        name="Defaults Provider",
        base_url="http://127.0.0.1:8000",
        api_format=PROTOCOL_CHAT_COMPLETIONS,
        provider_id="defaults_p",
        auth_mode=AUTH_NONE,
        transport_profile=TRANSPORT_GENERIC,
    )


def test_gateway_and_provider_live_config_defaults_are_unbounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentic_debugger.application.model_gateway import ModelGateway
    from agentic_debugger.application.model_providers import resolve_provider_live_config

    _isolate_loopback_provider(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTIC_DEBUGGER_CONFIG_DIR", str(tmp_path / "cfg"))

    # Generic omission itself is unbounded (signature defaults).
    assert inspect.signature(ModelGateway.resolve).parameters["logical_call_ceiling"].default == 0
    assert inspect.signature(ModelGateway.create_transport).parameters["max_model_requests"].default == 0
    assert inspect.signature(ModelGateway.create_transport).parameters["max_controller_steps"].default == 0
    assert inspect.signature(resolve_provider_live_config).parameters["logical_call_ceiling"].default == 0

    # Behavioral: omitted ceiling resolves to the unbounded argv.
    gateway = ModelGateway(config_root=tmp_path / "cfg")
    binding = gateway.resolve("defaults_p", "defaults-model-x")
    live, _ = resolve_provider_live_config("defaults_p", "defaults-model-x")
    assert _argv_ceiling(live.command) == "0"

    # The binding fingerprint matches the unbounded resolution.
    live0, _ = resolve_provider_live_config(
        "defaults_p", "defaults-model-x", logical_call_ceiling=0
    )
    assert binding.config_fingerprint == live0.configuration_fingerprint

    # Explicit finite still honored.
    live32, _ = resolve_provider_live_config(
        "defaults_p", "defaults-model-x", logical_call_ceiling=32
    )
    assert _argv_ceiling(live32.command) == "32"
    assert live32.configuration_fingerprint != live0.configuration_fingerprint


# ---------------------------------------------------------------------------
# builder omission → unbounded argv (commandcode / opencode / ollama)
# ---------------------------------------------------------------------------


def test_builder_omission_yields_unbounded_argv() -> None:
    cmd_cfg = commandcode_adapter.build_commandcode_live_config("muse/muse-spark-1.3")
    assert _argv_ceiling(cmd_cfg.command) == "0"
    commandcode_adapter.validate_logical_call_index(_valid_request_0based(100), 0)

    oc_cfg = opencode_adapter.build_opencode_live_config("opencode-go/glm-5.3")
    assert _argv_ceiling(oc_cfg.command) == "0"
    opencode_adapter.validate_logical_call_index(_valid_request_0based(100), 0)

    ol_cfg = ollama_adapter.build_ollama_live_config("gpt-oss:20b-cloud")
    assert _argv_ceiling(ol_cfg.command) == "0"
    ollama_adapter.validate_logical_call_index(_valid_request_0based(100), 0)


# ---------------------------------------------------------------------------
# run_adapter signature defaults → unbounded, accepted past old defaults
# ---------------------------------------------------------------------------


def test_run_adapter_signature_defaults_are_unbounded() -> None:
    for func in (
        commandcode_adapter.run_adapter,
        opencode_adapter.run_adapter,
        direct_adapter.run_adapter,
    ):
        default = inspect.signature(func).parameters["max_logical_calls"].default
        assert default == 0, f"{func.__module__} default is {default!r}, not unbounded"

    # Indices beyond every historical default (25/32/64) are accepted at
    # the omitted default on the 0-based family.
    commandcode_adapter.validate_logical_call_index(_valid_request_0based(200), 0)
    opencode_adapter.validate_logical_call_index(_valid_request_0based(200), 0)
    direct_adapter.validate_logical_call_index(_valid_request_0based(200), 0)
    # ... and on the 1-based family.
    assert opencode_go_adapter.validate_logical_call_index(_valid_request_1based(200), 0) is None
    assert agy_adapter.validate_logical_call_index(_valid_request_1based(200), 0) is None


# ---------------------------------------------------------------------------
# representative CLI black-box: omitted flag accepts index 200
# ---------------------------------------------------------------------------


def test_commandcode_cli_omitted_ceiling_accepts_index_beyond_old_default(
    tmp_path: Path,
) -> None:
    """Omit ``--max-logical-model-calls`` entirely with a high-index
    request on stdin: the run must proceed PAST the logical-call gate
    (failing later at executable resolution, never with
    ``logical_call_limit``).  The explicit-finite control still rejects.
    """
    request = json.dumps({"protocol": {"logical_model_call_index": 200}}).encode()
    base = [
        sys.executable, str(REPO_ROOT / "scripts" / "commandcode_goat_adapter.py"),
        "--model", "muse/muse-spark-1.3",
        "--timeout", "5",
        "--cmdc-executable", str(tmp_path / "nonexistent-cmdc"),
    ]
    omitted = subprocess.run(
        base, input=request, capture_output=True, timeout=60
    )
    assert omitted.returncode == 1
    assert b"logical_call_limit" not in omitted.stderr, omitted.stderr.decode(
        "utf-8", errors="replace"
    )

    control = subprocess.run(
        base + ["--max-logical-model-calls", "5"],
        input=request, capture_output=True, timeout=60,
    )
    assert control.returncode == 1
    assert b"logical_call_limit" in control.stderr


# ---------------------------------------------------------------------------
# explicit finite behavior preserved (harness + frozen treatment)
# ---------------------------------------------------------------------------


def test_explicit_finite_controller_harness_still_terminates_at_n() -> None:
    steps = tuple(
        ScriptedModelStep(
            ControllerState.UNDERSTAND,
            TransitionDirective(ControllerState.UNDERSTAND, f"churn {i}"),
        )
        for i in range(5)
    )
    adapter = ScriptedModelAdapter(steps)
    result = DeterministicController(
        ToolRegistry(), adapter, ControllerRunConfig(2)
    ).run(
        ControllerSnapshot(
            "run-fin", "task-fin", ControllerState.UNDERSTAND, 0,
            ControllerBudgetLimits(1, 1, 0), ControllerBudgetState(), HypothesisLedger(),
        )
    )
    assert result.stop_reason is ControllerStopReason.MODEL_CALL_LIMIT
    assert result.model_calls == 2


def test_explicit_finite_adapter_budget_still_terminates() -> None:
    task = load_task(
        str(REPO_ROOT / "agentic_debugger" / "datasets" / "curated"
            / "curated-off-by-one-002" / "task.json")
    )

    class FakeTransport:
        def request(self, payload, timeout_seconds):
            return {
                "directive": {
                    "kind": "transition",
                    "target_state": "Understand",
                    "reason": "churn",
                },
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

    from agentic_debugger.evaluation.live import LiveModelAdapterError

    adapter = LiveModelAdapter(
        task=task,
        policy=DemoPolicy.STATIC_BASELINE,
        config=LiveModelConfig(model_name="m", command=("echo", "hi"),
                               request_timeout_seconds=30, tool_version="t"),
        transport=FakeTransport(),
        limits=LiveRunLimits(max_model_requests=2, max_controller_steps=2,
                             max_retries=0, max_directive_repairs=0),
        registry=ToolRegistry(()),
        evaluation_id="e", case_id="c", run_id="r", trajectory_id="t",
    )
    snapshot = ControllerSnapshot(
        "r", task.task_id, ControllerState.UNDERSTAND, 0,
        ControllerBudgetLimits.from_task_constraints(task.constraints),
        ControllerBudgetState(), HypothesisLedger(),
    )
    adapter.next_directive(snapshot)
    adapter.next_directive(snapshot)
    with pytest.raises(LiveModelAdapterError, match="live model request limit reached"):
        adapter.next_directive(snapshot)
    assert adapter.metrics.termination_reason == "model_request_limit"


def test_explicit_frozen_treatment_envelope_still_constructible() -> None:
    budget = LiveTreatmentBudget(max_retries=1)
    assert budget.max_model_requests == 40
    assert budget.max_controller_steps == 40
    limits = LiveRunLimits(
        max_model_requests=budget.max_model_requests,
        max_controller_steps=budget.max_controller_steps,
        max_retries=budget.max_retries,
        treatment_budget=budget,
        max_directive_repairs=0,
    )
    assert limits.max_model_requests == 40
    assert limits.max_controller_steps == 40
