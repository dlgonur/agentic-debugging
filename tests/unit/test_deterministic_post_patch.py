"""Successful Session Token Efficiency v1: deterministic post-patch validation.

Offline scripted proof that the optimized controller reaches the same
authoritative RESOLVED result with materially fewer model requests,
while preserving recovery, PDB, verifier authority, and instrumentation.

No live provider is contacted; all transports are scripted/fake.
"""

from __future__ import annotations

from typing import Any, Callable

from agentic_debugger.agent.controller import (
    ControllerRunConfig,
    ControllerStopReason,
    DeterministicController,
)
from agentic_debugger.agent.controller_policy import (
    ActionName,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisConfidence,
    HypothesisLedger,
)
from agentic_debugger.agent.model_adapter import (
    ActionDirective,
    AddHypothesisDirective,
    ControllerSnapshot,
    ModelDirective,
    ScriptedModelAdapter,
    ScriptedModelStep,
    TransitionDirective,
)
from agentic_debugger.agent.observer import ControllerObservationKind
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.token_usage import TokenUsage
from agentic_debugger.agent.tool_registry import ToolRegistry, ToolResult, ToolSpec
from agentic_debugger.events.schema import ObservationStatus


LIMITS = ControllerBudgetLimits(
    max_patch_attempts=3,
    max_test_runs=5,
    max_pdb_observations=5,
    max_active_hypotheses=3,
    max_source_observations=10,
)

PATCH_TEXT = (
    "--- a/cookiecutter/config.py\n"
    "+++ b/cookiecutter/config.py\n"
    "@@ -1,3 +1,3 @@\n"
    "-old\n"
    "+new\n"
    " context\n"
)


def _snapshot(state: ControllerState = ControllerState.REPRODUCE, index: int = 0):
    return ControllerSnapshot(
        "run-eff", "task-eff", state, index, LIMITS,
        ControllerBudgetState(), HypothesisLedger(), None,
    )


def _validator(arguments: dict[str, object]) -> dict[str, object]:
    return dict(arguments)


def _ok(payload: dict[str, Any], summary: str) -> ToolResult:
    return ToolResult(ObservationStatus.OK, payload, summary)


def _build_success_registry(calls: list[str]) -> ToolRegistry:
    """Fake deterministic registry mimicking the happy-path demo tools."""

    def _record(name: str):
        def _handler(action, arguments):
            calls.append(action.name)
            if name == ActionName.RUN_REPRODUCTION.value:
                phase = arguments.get("phase")
                if phase == "baseline":
                    return _ok(
                        {"phase": "baseline", "failure_reproduced": True, "passed": False},
                        "baseline reproduction executed",
                    )
                return _ok(
                    {"phase": "post_patch", "passed": True, "failure_reproduced": False},
                    "post-patch reproduction executed",
                )
            if name == ActionName.FIND_FUNCTION.value:
                return _ok({"start_line": 10}, "declared symbol located")
            if name == ActionName.GET_SOURCE_WINDOW.value:
                return _ok({"window": "source"}, "source window retrieved")
            if name == ActionName.EXPRESS_ROOT_CAUSE_HYPOTHESIS.value:
                return _ok(dict(arguments), "root-cause hypothesis recorded")
            if name == ActionName.APPLY_PATCH.value:
                return _ok(
                    {"applied": True, "changed_files": ["cookiecutter/config.py"]},
                    "candidate patch applied",
                )
            if name == ActionName.SYNTAX_CHECK.value:
                return _ok({"all_passed": True, "results": []}, "syntax validated")
            if name == ActionName.RUN_REGRESSION_TESTS.value:
                return _ok({"all_passed": True, "node_count": 1}, "regression executed")
            if name == ActionName.CLASSIFY_OUTCOME.value:
                return _ok(
                    {"outcome": "RESOLVED", "f2p_passed": [True], "p2p_passed": [True]},
                    "outcome classified",
                )
            if name == ActionName.START_PDB_SESSION.value:
                return _ok({"state": "paused"}, "pdb started")
            if name == ActionName.GET_STACK_SUMMARY.value:
                return _ok({"pause_generation": 1, "frames": []}, "stack collected")
            if name == ActionName.GET_FRAME_LOCALS.value:
                return _ok({"locals": [], "pause_generation": 1}, "locals collected")
            if name == ActionName.STOP_PDB_SESSION.value:
                return _ok({"stopped": True}, "pdb stopped")
            return _ok({"received": arguments}, "ok")

        return _handler

    names = [
        ActionName.RUN_REPRODUCTION,
        ActionName.FIND_FUNCTION,
        ActionName.GET_SOURCE_WINDOW,
        ActionName.EXPRESS_ROOT_CAUSE_HYPOTHESIS,
        ActionName.APPLY_PATCH,
        ActionName.SYNTAX_CHECK,
        ActionName.RUN_REGRESSION_TESTS,
        ActionName.CLASSIFY_OUTCOME,
        ActionName.START_PDB_SESSION,
        ActionName.GET_STACK_SUMMARY,
        ActionName.GET_FRAME_LOCALS,
        ActionName.STOP_PDB_SESSION,
    ]
    return ToolRegistry(tuple(ToolSpec(name, _validator, _record(name.value)) for name in names))


def _full_semantic_script() -> tuple[ScriptedModelStep, ...]:
    """14-request representative successful path (control)."""
    return (
        ScriptedModelStep(
            ControllerState.REPRODUCE,
            ActionDirective(ActionName.RUN_REPRODUCTION, {"phase": "baseline"}),
        ),
        ScriptedModelStep(
            ControllerState.REPRODUCE,
            TransitionDirective(ControllerState.UNDERSTAND, "failure reproduced"),
        ),
        ScriptedModelStep(
            ControllerState.UNDERSTAND,
            ActionDirective(
                ActionName.FIND_FUNCTION,
                {"name": "load_config", "path": "cookiecutter/config.py"},
            ),
        ),
        ScriptedModelStep(
            ControllerState.UNDERSTAND,
            ActionDirective(
                ActionName.GET_SOURCE_WINDOW,
                {"path": "cookiecutter/config.py", "line": 10},
            ),
        ),
        ScriptedModelStep(
            ControllerState.UNDERSTAND,
            AddHypothesisDirective(
                "h-1", "off-by-one in config loader", HypothesisConfidence.LOW,
            ),
        ),
        ScriptedModelStep(
            ControllerState.UNDERSTAND,
            ActionDirective(
                ActionName.EXPRESS_ROOT_CAUSE_HYPOTHESIS,
                {
                    "hypothesis_id": "h-1",
                    "statement": "off-by-one in config loader",
                    "target_file": "cookiecutter/config.py",
                    "target_symbol": "load_config",
                    "confidence": "low",
                },
            ),
        ),
        ScriptedModelStep(
            ControllerState.UNDERSTAND,
            TransitionDirective(ControllerState.PATCH, "diagnosis ready"),
        ),
        ScriptedModelStep(
            ControllerState.PATCH,
            ActionDirective(ActionName.APPLY_PATCH, {"patch": PATCH_TEXT}),
        ),
        ScriptedModelStep(ControllerState.PATCH, ActionDirective(ActionName.SYNTAX_CHECK, {})),
        ScriptedModelStep(
            ControllerState.PATCH,
            TransitionDirective(ControllerState.VALIDATE, "syntax validated"),
        ),
        ScriptedModelStep(
            ControllerState.VALIDATE,
            ActionDirective(ActionName.RUN_REPRODUCTION, {"phase": "post_patch"}),
        ),
        ScriptedModelStep(
            ControllerState.VALIDATE, ActionDirective(ActionName.RUN_REGRESSION_TESTS, {})
        ),
        ScriptedModelStep(
            ControllerState.VALIDATE, ActionDirective(ActionName.CLASSIFY_OUTCOME, {})
        ),
        ScriptedModelStep(
            ControllerState.VALIDATE,
            TransitionDirective(ControllerState.DONE, "resolved"),
        ),
    )


class _ReactiveAdapter:
    """Model double that runs the semantic prefix then stops (optimized path)."""

    def __init__(self, step_fn: Callable[[ControllerSnapshot], ModelDirective]) -> None:
        self._step_fn = step_fn
        self.calls = 0
        self.states: list[ControllerState] = []

    def next_directive(self, snapshot: ControllerSnapshot) -> ModelDirective:
        self.calls += 1
        self.states.append(snapshot.state)
        return self._step_fn(snapshot)


def _prefix_policy() -> Callable[[ControllerSnapshot], ModelDirective]:
    phase = {"index": 0}
    script = list(_full_semantic_script()[:8])

    def _fn(snapshot: ControllerSnapshot) -> ModelDirective:
        # Optimized path must never be asked for mechanical post-patch work.
        # The 8 semantic directives end with APPLY_PATCH; any further call
        # proves a mechanical model turn was not eliminated.
        if phase["index"] >= len(script):
            raise AssertionError(
                f"model re-engaged after successful patch (state={snapshot.state.value}); "
                "deterministic continuation should have completed validation"
            )
        expected = script[phase["index"]]
        assert expected.expected_state is snapshot.state, (
            f"script state mismatch at prefix index {phase['index']}: "
            f"expected {expected.expected_state.value}, got {snapshot.state.value}"
        )
        phase["index"] += 1
        return expected.directive

    return _fn


class _Observer:
    def __init__(self) -> None:
        self.observations: list = []

    def notify(self, observation) -> None:
        self.observations.append(observation)


def test_before_after_successful_path_saves_requests_with_same_gates():
    """Control (flag False) needs 14 requests; optimized (flag True) needs 8."""
    before_calls: list[str] = []
    before_registry = _build_success_registry(before_calls)
    before = DeterministicController(
        before_registry,
        ScriptedModelAdapter(_full_semantic_script()),
        ControllerRunConfig(max_model_calls=32, deterministic_post_patch_validation=False),
    ).run(_snapshot())
    assert before.final_state is ControllerState.DONE
    assert before.stop_reason is ControllerStopReason.DONE
    assert before.model_calls == 14
    assert len(before_calls) == 9  # 9 tool dispatches in the happy path

    after_calls: list[str] = []
    after_registry = _build_success_registry(after_calls)
    adapter = _ReactiveAdapter(_prefix_policy())
    after = DeterministicController(
        after_registry,
        adapter,
        ControllerRunConfig(max_model_calls=32, deterministic_post_patch_validation=True),
    ).run(_snapshot())
    assert after.final_state is ControllerState.DONE
    assert after.stop_reason is ControllerStopReason.DONE
    # Same candidate semantics, same gates, same tool sequence.
    assert after_calls == before_calls
    assert after.budget_state == before.budget_state
    # Materially fewer model requests: 14 -> 8 (42% reduction, <=12 target).
    assert adapter.calls == 8
    assert after.model_calls == 8
    assert after.model_calls <= 12
    assert (before.model_calls - after.model_calls) / before.model_calls >= 0.30
    # Deterministic steps carry no model directive and consume no request.
    det_steps = [s for s in after.steps if s.deterministic]
    assert len(det_steps) == 6
    assert all(s.directive_kind is None for s in det_steps)
    assert all(s.action is not None or s.transition_reason is not None for s in det_steps)
    # Nondeterministic steps still equal model calls (honest accounting).
    nondet = [s for s in after.steps if not s.deterministic]
    assert len(nondet) == after.model_calls


def test_no_model_request_after_successful_patch_application():
    """Hard target: zero model requests whose only purpose is deterministic validation."""
    calls: list[str] = []
    registry = _build_success_registry(calls)
    adapter = _ReactiveAdapter(_prefix_policy())
    observer = _Observer()
    result = DeterministicController(
        registry,
        adapter,
        ControllerRunConfig(max_model_calls=32, deterministic_post_patch_validation=True),
        observer=observer,  # type: ignore[arg-type]
    ).run(_snapshot())
    assert result.final_state is ControllerState.DONE
    # Model was asked exactly through APPLY_PATCH (8 calls); the 6
    # mechanical validation steps produced tool/transition observations
    # but no MODEL_REQUEST_STARTED/COMPLETED pair.
    model_starts = [
        o for o in observer.observations
        if o.kind is ControllerObservationKind.MODEL_REQUEST_STARTED
    ]
    assert len(model_starts) == 8
    tool_starts = [
        o for o in observer.observations
        if o.kind is ControllerObservationKind.TOOL_STARTED
    ]
    assert len(tool_starts) == 9


def _registry_with(overrides: dict[str, Callable]) -> tuple[ToolRegistry, list[str]]:
    calls: list[str] = []
    base = _build_success_registry(calls)
    # Rebuild with overridden handlers for failure injection.
    specs = []
    for spec in base.specs:
        if spec.name.value in overrides:
            handler = overrides[spec.name.value]
            specs.append(ToolSpec(spec.name, _validator, handler))
        else:
            specs.append(spec)
    return ToolRegistry(tuple(specs)), calls


def test_syntax_failure_returns_to_model_for_revised_repair():
    attempts = {"syntax": 0}

    def _syntax(action, arguments):
        attempts["syntax"] += 1
        if attempts["syntax"] == 1:
            return _ok({"all_passed": False, "results": []}, "syntax failed")
        return _ok({"all_passed": True, "results": []}, "syntax validated")

    registry, _ = _registry_with({ActionName.SYNTAX_CHECK.value: _syntax})

    seen: list[str] = []

    def _policy(snapshot: ControllerSnapshot) -> ModelDirective:
        if snapshot.state is ControllerState.PATCH and snapshot.last_observation is None:
            return ActionDirective(ActionName.APPLY_PATCH, {"patch": PATCH_TEXT})
        if snapshot.state is ControllerState.PATCH:
            # Deterministic syntax ran (no new model request for the tool
            # itself); the failure evidence must reach the model here.
            obs = snapshot.last_observation
            assert obs is not None and obs.name == ActionName.SYNTAX_CHECK.value
            assert obs.status is ObservationStatus.OK
            if obs.payload.get("all_passed") is False:
                seen.append("syntax-feedback")
                return ActionDirective(ActionName.APPLY_PATCH, {"patch": PATCH_TEXT + "# retry\n"})
            raise AssertionError("second syntax should have passed deterministically")
        raise AssertionError(f"unexpected state {snapshot.state}")

    result = DeterministicController(
        registry,
        _ReactiveAdapter(_policy),
        ControllerRunConfig(max_model_calls=16, deterministic_post_patch_validation=True),
    ).run(ControllerSnapshot(
        "r1", "t1", ControllerState.PATCH, 0, LIMITS, ControllerBudgetState(),
        HypothesisLedger(), None,
    ))
    # First syntax failed and re-engaged the model; the retry then passed
    # syntax and completed deterministically to DONE.
    assert seen == ["syntax-feedback"]
    assert result.final_state is ControllerState.DONE
    assert result.stop_reason is ControllerStopReason.DONE
    det_syntax_steps = [
        s for s in result.steps
        if s.deterministic and s.action is not None and s.action.name == ActionName.SYNTAX_CHECK.value
    ]
    assert len(det_syntax_steps) == 2
    assert det_syntax_steps[0].observation.payload.get("all_passed") is False
    assert det_syntax_steps[1].observation.payload.get("all_passed") is True


def test_reproduction_still_failing_does_not_become_resolved():
    def _repro(action, arguments):
        if arguments.get("phase") == "post_patch":
            return _ok({"phase": "post_patch", "passed": False}, "still failing")
        return _ok({"phase": "baseline", "passed": False}, "baseline")

    def _classify(action, arguments):
        return _ok({"outcome": "NO_OP", "f2p_passed": [False]}, "classified")

    registry, _ = _registry_with({
        ActionName.RUN_REPRODUCTION.value: _repro,
        ActionName.CLASSIFY_OUTCOME.value: _classify,
    })

    def _policy(snapshot: ControllerSnapshot) -> ModelDirective:
        if snapshot.last_observation is None:
            return ActionDirective(ActionName.APPLY_PATCH, {"patch": PATCH_TEXT})
        # After deterministic repro+regression+classify with NO_OP, the
        # model must be re-engaged (not auto-completed).
        obs = snapshot.last_observation
        assert obs is not None and obs.name == ActionName.CLASSIFY_OUTCOME.value
        assert obs.payload.get("outcome") == "NO_OP"
        return TransitionDirective(ControllerState.FAILED, "candidate does not fix failure")

    result = DeterministicController(
        registry,
        _ReactiveAdapter(_policy),
        ControllerRunConfig(max_model_calls=8, deterministic_post_patch_validation=True),
    ).run(ControllerSnapshot(
        "r2", "t1", ControllerState.PATCH, 0, LIMITS, ControllerBudgetState(),
        HypothesisLedger(), None,
    ))
    assert result.final_state is ControllerState.FAILED
    assert result.stop_reason is ControllerStopReason.FAILED


def test_regression_failure_returns_to_model():
    def _regression(action, arguments):
        return _ok({"all_passed": False}, "regression failed")

    def _classify(action, arguments):
        return _ok({"outcome": "BREAKING_RESOLVED"}, "classified")

    registry, _ = _registry_with({
        ActionName.RUN_REGRESSION_TESTS.value: _regression,
        ActionName.CLASSIFY_OUTCOME.value: _classify,
    })

    def _policy(snapshot: ControllerSnapshot) -> ModelDirective:
        if snapshot.last_observation is None:
            return ActionDirective(ActionName.APPLY_PATCH, {"patch": PATCH_TEXT})
        obs = snapshot.last_observation
        assert obs is not None and obs.name == ActionName.CLASSIFY_OUTCOME.value
        assert obs.payload.get("outcome") == "BREAKING_RESOLVED"
        return TransitionDirective(ControllerState.FAILED, "breaking change")

    result = DeterministicController(
        registry,
        _ReactiveAdapter(_policy),
        ControllerRunConfig(max_model_calls=8, deterministic_post_patch_validation=True),
    ).run(ControllerSnapshot(
        "r3", "t1", ControllerState.PATCH, 0, LIMITS, ControllerBudgetState(),
        HypothesisLedger(), None,
    ))
    assert result.final_state is ControllerState.FAILED
    # Deterministic continuation never converts a failed verifier-equivalent
    # classification into success.
    assert result.stop_reason is ControllerStopReason.FAILED


def test_recoverable_patch_rejection_reaches_model_with_feedback():
    from agentic_debugger.agent.tool_registry import ToolRejectedError

    def _apply(action, arguments):
        raise ToolRejectedError(
            "context mismatch",
            safe_diagnostic="context mismatch at line 10",
            recoverable=True,
            payload_data={"patch_failure": {"kind": "context_mismatch", "recoverable": True}},
        )

    registry, _ = _registry_with({ActionName.APPLY_PATCH.value: _apply})

    def _policy(snapshot: ControllerSnapshot) -> ModelDirective:
        if snapshot.last_observation is None:
            return ActionDirective(ActionName.APPLY_PATCH, {"patch": "bad"})
        obs = snapshot.last_observation
        assert obs.status is ObservationStatus.REJECTED
        assert obs.payload.get("recoverable") is True
        return TransitionDirective(ControllerState.FAILED, "cannot apply")

    result = DeterministicController(
        registry,
        _ReactiveAdapter(_policy),
        ControllerRunConfig(max_model_calls=4, deterministic_post_patch_validation=True),
    ).run(ControllerSnapshot(
        "r4", "t1", ControllerState.PATCH, 0, LIMITS, ControllerBudgetState(),
        HypothesisLedger(), None,
    ))
    assert result.final_state is ControllerState.FAILED
    # No deterministic steps run when the patch itself was not accepted.
    assert all(not s.deterministic for s in result.steps)


def test_non_recoverable_infrastructure_failure_terminates_without_retry():
    from agentic_debugger.agent.tool_registry import ToolExecutionError

    def _apply(action, arguments):
        raise ToolExecutionError(
            "disk write failed",
            safe_diagnostic="disk write failed",
            recoverable=False,
            payload_data={"patch_failure": {"kind": "write_failure", "recoverable": False}},
        )

    registry, _ = _registry_with({ActionName.APPLY_PATCH.value: _apply})
    calls = {"n": 0}

    def _policy(snapshot: ControllerSnapshot) -> ModelDirective:
        calls["n"] += 1
        assert calls["n"] == 1, "model must not be re-engaged after fatal infrastructure failure"
        return ActionDirective(ActionName.APPLY_PATCH, {"patch": PATCH_TEXT})

    result = DeterministicController(
        registry,
        _ReactiveAdapter(_policy),
        ControllerRunConfig(max_model_calls=4, deterministic_post_patch_validation=True),
    ).run(ControllerSnapshot(
        "r5", "t1", ControllerState.PATCH, 0, LIMITS, ControllerBudgetState(),
        HypothesisLedger(), None,
    ))
    assert result.final_state is ControllerState.FAILED
    assert result.stop_reason is ControllerStopReason.FAILED
    assert calls["n"] == 1
    assert result.model_calls == 1


def test_fatal_syntax_infrastructure_terminates_without_model_retry():
    from agentic_debugger.agent.tool_registry import ToolExecutionError

    def _syntax(action, arguments):
        raise ToolExecutionError(
            "syntax infra failed",
            safe_diagnostic="syntax infra failed",
            recoverable=False,
            payload_data={"patch_failure": {"kind": "syntax_check_failure", "recoverable": False}},
        )

    registry, _ = _registry_with({ActionName.SYNTAX_CHECK.value: _syntax})
    calls = {"n": 0}

    def _policy(snapshot: ControllerSnapshot) -> ModelDirective:
        calls["n"] += 1
        if calls["n"] > 1:
            raise AssertionError("model must not be re-engaged after fatal syntax infra failure")
        return ActionDirective(ActionName.APPLY_PATCH, {"patch": PATCH_TEXT})

    result = DeterministicController(
        registry,
        _ReactiveAdapter(_policy),
        ControllerRunConfig(max_model_calls=4, deterministic_post_patch_validation=True),
    ).run(ControllerSnapshot(
        "r6", "t1", ControllerState.PATCH, 0, LIMITS, ControllerBudgetState(),
        HypothesisLedger(), None,
    ))
    assert result.final_state is ControllerState.FAILED
    assert result.stop_reason is ControllerStopReason.FAILED
    assert calls["n"] == 1


def test_pdb_required_path_still_uses_pdb_before_deterministic_validation():
    calls: list[str] = []
    registry = _build_success_registry(calls)
    pdb_done = {"value": False}

    def _policy(snapshot: ControllerSnapshot) -> ModelDirective:
        obs_name = snapshot.last_observation.name if snapshot.last_observation else None
        if snapshot.state is ControllerState.UNDERSTAND:
            if not snapshot.hypotheses.active_hypotheses():
                return AddHypothesisDirective(
                    "h-1", "needs runtime", HypothesisConfidence.LOW, (), True,
                )
            if not pdb_done["value"]:
                return TransitionDirective(ControllerState.RUNTIME_EVIDENCE, "need pdb")
            return TransitionDirective(ControllerState.PATCH, "pdb done")
        if snapshot.state is ControllerState.RUNTIME_EVIDENCE:
            if obs_name is None:
                return ActionDirective(ActionName.START_PDB_SESSION, {})
            if obs_name == ActionName.START_PDB_SESSION.value:
                return ActionDirective(ActionName.GET_STACK_SUMMARY, {})
            if obs_name == ActionName.GET_STACK_SUMMARY.value:
                return ActionDirective(
                    ActionName.GET_FRAME_LOCALS, {"frame_id": 0, "pause_generation": 1}
                )
            if obs_name == ActionName.GET_FRAME_LOCALS.value:
                return ActionDirective(ActionName.STOP_PDB_SESSION, {})
            if obs_name == ActionName.STOP_PDB_SESSION.value:
                pdb_done["value"] = True
                return TransitionDirective(ControllerState.UNDERSTAND, "pdb collected")
        if snapshot.state is ControllerState.PATCH and obs_name != ActionName.APPLY_PATCH.value:
            # After PDB evidence, propose the patch; deterministic handles the rest.
            return ActionDirective(ActionName.APPLY_PATCH, {"patch": PATCH_TEXT})
        raise AssertionError(f"model should not be re-engaged after patch: {snapshot.state}")

    # Start in UNDERSTAND with no hypothesis to force the PDB visit.
    start = ControllerSnapshot(
        "r-pdb", "t1", ControllerState.UNDERSTAND, 0, LIMITS,
        ControllerBudgetState(), HypothesisLedger(), None,
    )
    # First transition UNDERSTAND->RUNTIME_EVIDENCE needs no PDB gate in this
    # unit harness (require_pdb_evidence_before_patch=False).
    result = DeterministicController(
        registry,
        _ReactiveAdapter(_policy),
        ControllerRunConfig(max_model_calls=16, deterministic_post_patch_validation=True),
    ).run(start)
    assert result.final_state is ControllerState.DONE
    assert "start_pdb_session" in calls
    assert "get_stack_summary" in calls
    assert "get_frame_locals" in calls
    assert result.budget_state.pdb_observations == 2


class _UsageAndSizeAdapter:
    """Fake live-like adapter reporting usage and request size per call."""

    def __init__(self, directives: list[ModelDirective], states: list[ControllerState]) -> None:
        self._directives = list(directives)
        self._states = list(states)
        self.calls = 0
        self._last_usage: TokenUsage | None = None
        self._last_bytes: int | None = None

    def next_directive(self, snapshot: ControllerSnapshot) -> ModelDirective:
        assert self._states[self.calls] is snapshot.state
        directive = self._directives[self.calls]
        self.calls += 1
        # Report distinct usage/size per logical call like a live adapter.
        self._last_usage = TokenUsage(
            input_tokens=1000 + self.calls * 10,
            output_tokens=200,
            cached_input_tokens=100,
            total_tokens=1200 + self.calls * 10,
        )
        self._last_bytes = 5000 + self.calls
        return directive

    def last_request_token_usage(self):  # type: ignore[no-untyped-def]
        return self._last_usage

    def last_request_payload_bytes(self):  # type: ignore[no-untyped-def]
        return self._last_bytes


def test_deterministic_work_preserves_model_request_ordinals():
    """Regression: deterministic steps must not consume model ordinals.

    Model request ordinals identify REAL model requests.  A scripted
    adapter indexes its script by ``snapshot.model_call_index`` and the
    live adapter keys its per-call caches the same way, so a gap created
    by deterministic work would break recovery paths (script mismatch /
    exhaustion) and misstate evidence.  Deterministic identities live in
    the disjoint det-action-/det-observation- namespace instead.
    """
    attempts = {"syntax": 0}

    def _syntax(action, arguments):
        attempts["syntax"] += 1
        if attempts["syntax"] == 1:
            return _ok({"all_passed": False, "results": []}, "syntax failed")
        return _ok({"all_passed": True, "results": []}, "syntax validated")

    registry, _ = _registry_with({ActionName.SYNTAX_CHECK.value: _syntax})
    scripted = ScriptedModelAdapter((
        ScriptedModelStep(
            ControllerState.PATCH,
            ActionDirective(ActionName.APPLY_PATCH, {"patch": PATCH_TEXT}),
        ),
        ScriptedModelStep(
            ControllerState.PATCH,
            ActionDirective(ActionName.APPLY_PATCH, {"patch": PATCH_TEXT + "# retry\n"}),
        ),
    ))
    observer = _Observer()
    result = DeterministicController(
        registry,
        scripted,
        ControllerRunConfig(max_model_calls=8, deterministic_post_patch_validation=True),
        observer=observer,  # type: ignore[arg-type]
    ).run(ControllerSnapshot(
        "r-ord", "t1", ControllerState.PATCH, 0, LIMITS, ControllerBudgetState(),
        HypothesisLedger(), None,
    ))
    # The retry script slot is index 1: if deterministic work had consumed
    # a model ordinal, the post-abort snapshot would carry index 2 and the
    # script would exhaust instead of completing.
    assert result.final_state is ControllerState.DONE
    assert result.stop_reason is ControllerStopReason.DONE
    assert result.model_calls == 2
    nondet = [s for s in result.steps if not s.deterministic]
    assert [s.model_call_index for s in nondet] == [0, 1]
    det = [s for s in result.steps if s.deterministic]
    assert len(det) == 7
    # Owning-request attribution, never new ordinals.
    assert det[0].model_call_index == 0
    assert all(s.model_call_index == 1 for s in det[1:])
    # Disjoint identity namespaces: no det identity collides with (or
    # mimics) a model-request identity, and every det identity is unique.
    model_ids = [s.action.action_id for s in nondet if s.action is not None]
    assert model_ids == ["action-000000000", "action-000000001"]
    det_ids = [s.action.action_id for s in det if s.action is not None]
    assert len(det_ids) == 5
    assert all(item.startswith("det-action-") for item in det_ids)
    assert len(set(det_ids)) == len(det_ids)
    assert not (set(det_ids) & set(model_ids))
    det_obs = [s.observation.observation_id for s in det if s.observation is not None]
    assert all(item.startswith("det-observation-") for item in det_obs)
    assert [item.replace("det-observation-", "") for item in det_obs] == [
        item.replace("det-action-", "") for item in det_ids
    ]
    # No MODEL_REQUEST event is fabricated for deterministic work: request
    # ordinals stay contiguous with no gaps.
    completed = [
        o for o in observer.observations
        if o.kind is ControllerObservationKind.MODEL_REQUEST_COMPLETED
    ]
    assert [o.model_call_index for o in completed] == [0, 1]


def test_request_instrumentation_carries_ordinal_stage_category_usage_and_size():
    calls: list[str] = []
    registry = _build_success_registry(calls)
    script = list(_full_semantic_script()[:8])
    states = [s.expected_state for s in script]
    directives = [s.directive for s in script]
    adapter = _UsageAndSizeAdapter(directives, states)
    observer = _Observer()
    result = DeterministicController(
        registry,
        adapter,
        ControllerRunConfig(max_model_calls=16, deterministic_post_patch_validation=True),
        observer=observer,  # type: ignore[arg-type]
    ).run(_snapshot())
    assert result.final_state is ControllerState.DONE
    completed = [
        o for o in observer.observations
        if o.kind is ControllerObservationKind.MODEL_REQUEST_COMPLETED
    ]
    # One completion per model request, in ordinal order.
    assert [o.model_call_index for o in completed] == list(range(8))
    # Each completion carries stage, usage, and size without secrets.
    for obs in completed:
        assert obs.state_before is not None
        assert obs.request_status == "ok"
        assert obs.token_usage is not None and obs.token_usage.reported
        assert obs.request_bytes is not None and obs.request_bytes > 0
    # Step records distinguish semantic vs deterministic categories.
    assert [s.directive_kind.value if s.directive_kind else None for s in result.steps[:8]] == [
        "action", "transition", "action", "action",
        "add_hypothesis", "action", "transition", "action",
    ]
    assert all(s.deterministic for s in result.steps[8:])
