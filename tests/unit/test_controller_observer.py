"""Focused tests for the controller-native typed observation seam.

Covers: no-observer equivalence, deterministic observation ordering,
accepted/rejected directive boundaries, tool success/failure paths,
transitions, hypothesis paths, every reachable terminal category, early
DONE/FAILED paths, identity on every observation, observer immutability,
and the bounded observer failure semantics.
"""

from __future__ import annotations

import pytest

from agentic_debugger.agent import controller as controller_module
from agentic_debugger.agent.controller import (
    ControllerInputError,
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
    ModelAdapterError,
    ScriptedModelAdapter,
    ScriptedModelStep,
    TransitionDirective,
)
from agentic_debugger.agent.observer import (
    ControllerObservation,
    ControllerObservationKind,
    ControllerObservationError,
    NoopControllerObserver,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import (
    ToolExecutionError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from agentic_debugger.events.schema import ObservationStatus


LIMITS = ControllerBudgetLimits(2, 3, 2, max_active_hypotheses=2, max_source_observations=3)


def snapshot(state=ControllerState.REPRODUCE, index=0, *, limits=LIMITS,
             budget=None, ledger=None, last=None):
    return ControllerSnapshot(
        "run-1", "task-1", state, index, limits,
        budget or ControllerBudgetState(), ledger or HypothesisLedger(), last,
    )


def registry_for(*names, handler=None):
    def default_handler(action, arguments):
        return ToolResult(ObservationStatus.OK, {"received": arguments}, "ok")

    def validator(arguments):
        return arguments

    return ToolRegistry(tuple(
        ToolSpec(name, validator, handler or default_handler) for name in names
    ))


def scripted(directives, states=None):
    states = states or [ControllerState.REPRODUCE] * len(directives)
    return ScriptedModelAdapter(tuple(
        ScriptedModelStep(state, directive)
        for state, directive in zip(states, directives)
    ))


class RecordingObserver:
    """Collects observations; optionally raises per notify."""

    def __init__(self, *, raise_exc=None, handler=None):
        self.observations = []
        self.raise_exc = raise_exc
        self.handler = handler

    def notify(self, observation):
        self.observations.append(observation)
        if self.handler is not None:
            self.handler(observation)
        if self.raise_exc is not None:
            raise self.raise_exc


HAPPY_STEPS = (
    ScriptedModelStep(ControllerState.REPRODUCE, ActionDirective(ActionName.RUN_TESTS, {})),
    ScriptedModelStep(ControllerState.REPRODUCE, TransitionDirective(ControllerState.UNDERSTAND, "failure reproduced")),
    ScriptedModelStep(ControllerState.UNDERSTAND, AddHypothesisDirective("h-1", "off by one", HypothesisConfidence.MEDIUM)),
    ScriptedModelStep(ControllerState.UNDERSTAND, TransitionDirective(ControllerState.PATCH, "candidate")),
    ScriptedModelStep(ControllerState.PATCH, ActionDirective(ActionName.APPLY_PATCH, {"patch": "diff"})),
    ScriptedModelStep(ControllerState.PATCH, TransitionDirective(ControllerState.VALIDATE, "applied")),
    ScriptedModelStep(ControllerState.VALIDATE, ActionDirective(ActionName.RUN_REPRODUCTION, {})),
    ScriptedModelStep(ControllerState.VALIDATE, TransitionDirective(ControllerState.DONE, "resolved")),
)

HAPPY_REGISTRY = registry_for(
    ActionName.RUN_TESTS, ActionName.APPLY_PATCH, ActionName.RUN_REPRODUCTION
)


def run_happy(observer=None):
    controller = DeterministicController(
        HAPPY_REGISTRY,
        ScriptedModelAdapter(tuple(HAPPY_STEPS)),
        ControllerRunConfig(max_model_calls=32),
        observer or NoopControllerObserver(),
    )
    return controller.run(snapshot())


class TestNoObserverCompatibility:
    def test_default_observer_is_noop_and_run_unchanged(self):
        default = DeterministicController(
            HAPPY_REGISTRY, ScriptedModelAdapter(tuple(HAPPY_STEPS))
        )
        assert isinstance(default.observer, NoopControllerObserver)
        assert default.run(snapshot()) == DeterministicController(
            HAPPY_REGISTRY, ScriptedModelAdapter(tuple(HAPPY_STEPS))
        ).run(snapshot())

    def test_observer_vs_no_observer_result_equality_happy_path(self):
        plain = run_happy()
        observed = run_happy(observer=RecordingObserver())
        assert observed == plain

    @pytest.mark.parametrize("script_kind", ["rejected", "tool_failure", "budget", "model_error"])
    def test_observer_vs_no_observer_result_equality_paths(self, script_kind):
        if script_kind == "rejected":
            adapter = scripted((ActionDirective(ActionName.APPLY_PATCH, {}),))
            registry = HAPPY_REGISTRY
        elif script_kind == "tool_failure":
            def failing(action, arguments):
                raise ToolExecutionError("boom")
            adapter = scripted((ActionDirective(ActionName.RUN_TESTS, {}),))
            registry = registry_for(ActionName.RUN_TESTS, handler=failing)
        elif script_kind == "budget":
            adapter = scripted((ActionDirective(ActionName.RUN_TESTS, {}),))
            registry = registry_for(ActionName.RUN_TESTS)
        else:
            class FailingAdapter:
                model_name = "failing"
                def next_directive(self, call_snapshot):
                    raise ModelAdapterError("model failed")
            adapter = FailingAdapter()
            registry = ToolRegistry()
        budget = (
            ControllerBudgetState(test_runs=LIMITS.max_test_runs)
            if script_kind == "budget"
            else None
        )
        plain = DeterministicController(
            registry, adapter, ControllerRunConfig(max_model_calls=4)
        ).run(snapshot(budget=budget))
        observed = DeterministicController(
            registry, adapter, ControllerRunConfig(max_model_calls=4),
            observer=RecordingObserver(),
        ).run(snapshot(budget=budget))
        assert observed == plain


class TestObservationOrdering:
    def test_happy_path_observation_order_is_deterministic(self):
        observer = RecordingObserver()
        result = run_happy(observer=observer)
        assert result.final_state is ControllerState.DONE
        assert result.stop_reason is ControllerStopReason.DONE
        assert [obs.kind for obs in observer.observations] == [
            ControllerObservationKind.RUN_STARTED,
            ControllerObservationKind.MODEL_REQUEST_STARTED,
            ControllerObservationKind.MODEL_REQUEST_COMPLETED,
            ControllerObservationKind.DIRECTIVE_ACCEPTED,
            ControllerObservationKind.TOOL_STARTED,
            ControllerObservationKind.TOOL_COMPLETED,
            ControllerObservationKind.STEP_COMPLETED,
            ControllerObservationKind.MODEL_REQUEST_STARTED,
            ControllerObservationKind.MODEL_REQUEST_COMPLETED,
            ControllerObservationKind.DIRECTIVE_ACCEPTED,
            ControllerObservationKind.STATE_TRANSITION,
            ControllerObservationKind.STEP_COMPLETED,
            ControllerObservationKind.MODEL_REQUEST_STARTED,
            ControllerObservationKind.MODEL_REQUEST_COMPLETED,
            ControllerObservationKind.DIRECTIVE_ACCEPTED,
            ControllerObservationKind.STEP_COMPLETED,
            ControllerObservationKind.MODEL_REQUEST_STARTED,
            ControllerObservationKind.MODEL_REQUEST_COMPLETED,
            ControllerObservationKind.DIRECTIVE_ACCEPTED,
            ControllerObservationKind.STATE_TRANSITION,
            ControllerObservationKind.STEP_COMPLETED,
            ControllerObservationKind.MODEL_REQUEST_STARTED,
            ControllerObservationKind.MODEL_REQUEST_COMPLETED,
            ControllerObservationKind.DIRECTIVE_ACCEPTED,
            ControllerObservationKind.TOOL_STARTED,
            ControllerObservationKind.TOOL_COMPLETED,
            ControllerObservationKind.STEP_COMPLETED,
            ControllerObservationKind.MODEL_REQUEST_STARTED,
            ControllerObservationKind.MODEL_REQUEST_COMPLETED,
            ControllerObservationKind.DIRECTIVE_ACCEPTED,
            ControllerObservationKind.STATE_TRANSITION,
            ControllerObservationKind.STEP_COMPLETED,
            ControllerObservationKind.MODEL_REQUEST_STARTED,
            ControllerObservationKind.MODEL_REQUEST_COMPLETED,
            ControllerObservationKind.DIRECTIVE_ACCEPTED,
            ControllerObservationKind.TOOL_STARTED,
            ControllerObservationKind.TOOL_COMPLETED,
            ControllerObservationKind.STEP_COMPLETED,
            ControllerObservationKind.MODEL_REQUEST_STARTED,
            ControllerObservationKind.MODEL_REQUEST_COMPLETED,
            ControllerObservationKind.DIRECTIVE_ACCEPTED,
            ControllerObservationKind.STATE_TRANSITION,
            ControllerObservationKind.STEP_COMPLETED,
            ControllerObservationKind.TERMINAL,
        ]

    def test_step_indices_match_result_ordinal(self):
        observer = RecordingObserver()
        result = run_happy(observer=observer)
        step_observations = [
            obs for obs in observer.observations
            if obs.kind is ControllerObservationKind.STEP_COMPLETED
        ]
        assert [obs.step_index for obs in step_observations] == list(range(len(result.steps)))
        assert [obs.directive_kind for obs in step_observations] == [
            "action", "transition", "add_hypothesis", "transition",
            "action", "transition", "action", "transition",
        ]
        assert [obs.stop_reason for obs in step_observations] == [
            None, None, None, None, None, None, None, "done"
        ]

    def test_exactly_one_terminal_observation(self):
        observer = RecordingObserver()
        run_happy(observer=observer)
        terminals = [
            obs for obs in observer.observations
            if obs.kind is ControllerObservationKind.TERMINAL
        ]
        assert len(terminals) == 1
        assert terminals[0].stop_reason == "done"
        assert terminals[0].state_after is ControllerState.DONE
        assert observer.observations[-1] is terminals[0]

    def test_model_request_fields(self):
        observer = RecordingObserver()
        run_happy(observer=observer)
        started = [
            obs for obs in observer.observations
            if obs.kind is ControllerObservationKind.MODEL_REQUEST_STARTED
        ]
        completed = [
            obs for obs in observer.observations
            if obs.kind is ControllerObservationKind.MODEL_REQUEST_COMPLETED
        ]
        assert [obs.model_call_index for obs in started] == [0, 1, 2, 3, 4, 5, 6, 7]
        assert [obs.request_status for obs in completed] == ["ok"] * 8
        assert all(obs.state_before is not None for obs in started)


class TestIdentity:
    def test_every_observation_carries_run_and_task_identity(self):
        observer = RecordingObserver()
        run_happy(observer=observer)
        assert len(observer.observations) > 0
        for observation in observer.observations:
            assert observation.run_id == "run-1"
            assert observation.task_id == "task-1"


class TestDirectiveBoundaries:
    def test_accepted_directive_precedes_tool_start(self):
        order = []
        observer = RecordingObserver(
            handler=lambda obs: order.append((obs.kind, obs.directive_kind))
        )
        run_happy(observer=observer)
        accepted_action = [
            index for index, (kind, directive_kind) in enumerate(order)
            if kind is ControllerObservationKind.DIRECTIVE_ACCEPTED
            and directive_kind == "action"
        ]
        tool_started = [
            index for index, (kind, _) in enumerate(order)
            if kind is ControllerObservationKind.TOOL_STARTED
        ]
        assert len(accepted_action) == len(tool_started) == 3
        for accepted_index, tool_index in zip(accepted_action, tool_started):
            assert accepted_index < tool_index

    def test_accepted_directive_then_tool_failure_stays_accepted(self):
        def failing(action, arguments):
            raise ToolExecutionError("tool blew up")
        registry = registry_for(ActionName.RUN_TESTS, handler=failing)
        adapter = scripted((
            ActionDirective(ActionName.RUN_TESTS, {}),
            TransitionDirective(ControllerState.UNDERSTAND, "proceed"),
        ))
        observer = RecordingObserver()
        result = DeterministicController(
            registry, adapter, ControllerRunConfig(max_model_calls=4),
            observer=observer,
        ).run(snapshot())
        assert result.final_state is ControllerState.FAILED
        kinds = [obs.kind for obs in observer.observations]
        assert ControllerObservationKind.DIRECTIVE_ACCEPTED in kinds
        assert ControllerObservationKind.DIRECTIVE_REJECTED not in kinds
        tool_completed = [
            obs for obs in observer.observations
            if obs.kind is ControllerObservationKind.TOOL_COMPLETED
        ]
        assert len(tool_completed) == 1
        assert tool_completed[0].observation_status is ObservationStatus.ERROR
        assert tool_completed[0].tool_name == "run_tests"
        assert observer.observations[-1].stop_reason == "model_script_exhausted"

    def test_state_action_rejection_is_rejected(self):
        observer = RecordingObserver()
        result = DeterministicController(
            HAPPY_REGISTRY,
            scripted((ActionDirective(ActionName.APPLY_PATCH, {}),)),
            observer=observer,
        ).run(snapshot())
        assert result.stop_reason is ControllerStopReason.DIRECTIVE_REJECTED
        rejected = [
            obs for obs in observer.observations
            if obs.kind is ControllerObservationKind.DIRECTIVE_REJECTED
        ]
        assert len(rejected) == 1
        assert rejected[0].rejection_category == "state_action_not_allowed"
        assert rejected[0].directive_kind == "action"

    def test_budget_rejection_is_rejected(self):
        observer = RecordingObserver()
        result = DeterministicController(
            registry_for(ActionName.RUN_TESTS),
            scripted((ActionDirective(ActionName.RUN_TESTS, {}),)),
            observer=observer,
        ).run(snapshot(budget=ControllerBudgetState(test_runs=LIMITS.max_test_runs)))
        assert result.stop_reason is ControllerStopReason.BUDGET_EXHAUSTED
        rejected = [
            obs for obs in observer.observations
            if obs.kind is ControllerObservationKind.DIRECTIVE_REJECTED
        ]
        assert len(rejected) == 1
        assert rejected[0].rejection_category == "budget_exhausted"

    def test_transition_rejection_is_rejected(self):
        observer = RecordingObserver()
        result = DeterministicController(
            ToolRegistry(),
            scripted((TransitionDirective(ControllerState.DONE, "skip"),)),
            observer=observer,
        ).run(snapshot())
        assert result.stop_reason is ControllerStopReason.DIRECTIVE_REJECTED
        rejected = [
            obs for obs in observer.observations
            if obs.kind is ControllerObservationKind.DIRECTIVE_REJECTED
        ]
        assert len(rejected) == 1
        assert rejected[0].rejection_category == "transition_not_allowed"

    def test_hypothesis_state_rejection_is_rejected(self):
        observer = RecordingObserver()
        result = DeterministicController(
            ToolRegistry(),
            scripted((AddHypothesisDirective("h-1", "stmt", HypothesisConfidence.HIGH),)),
            observer=observer,
        ).run(snapshot())
        assert result.stop_reason is ControllerStopReason.DIRECTIVE_REJECTED
        rejected = [
            obs for obs in observer.observations
            if obs.kind is ControllerObservationKind.DIRECTIVE_REJECTED
        ]
        assert len(rejected) == 1
        assert rejected[0].rejection_category == "state_not_allowed"

    def test_hypothesis_policy_rejection_is_rejected(self):
        observer = RecordingObserver()
        adapter = scripted((
            AddHypothesisDirective("h-1", "stmt", HypothesisConfidence.HIGH),
            AddHypothesisDirective("h-1", "stmt again", HypothesisConfidence.HIGH),
        ), states=[ControllerState.UNDERSTAND, ControllerState.UNDERSTAND])
        result = DeterministicController(
            ToolRegistry(), adapter, observer=observer,
        ).run(snapshot(ControllerState.UNDERSTAND))
        assert result.stop_reason is ControllerStopReason.DIRECTIVE_REJECTED
        rejected = [
            obs for obs in observer.observations
            if obs.kind is ControllerObservationKind.DIRECTIVE_REJECTED
        ]
        assert len(rejected) == 1
        assert rejected[0].rejection_category == "policy_rejected"

    def test_accepted_transition_carries_target_state(self):
        observer = RecordingObserver()
        DeterministicController(
            ToolRegistry(),
            scripted((TransitionDirective(ControllerState.UNDERSTAND, "proceed"),)),
            observer=observer,
        ).run(snapshot())
        accepted = [
            obs for obs in observer.observations
            if obs.kind is ControllerObservationKind.DIRECTIVE_ACCEPTED
        ]
        assert len(accepted) == 1
        assert accepted[0].target_state is ControllerState.UNDERSTAND
        assert accepted[0].directive_kind == "transition"


class TestTransitionsAndTerminals:
    def test_transition_observable_with_state_change(self):
        observer = RecordingObserver()
        DeterministicController(
            ToolRegistry(),
            scripted((TransitionDirective(ControllerState.UNDERSTAND, "proceed"),)),
            observer=observer,
        ).run(snapshot())
        transitions = [
            obs for obs in observer.observations
            if obs.kind is ControllerObservationKind.STATE_TRANSITION
        ]
        # The accepted transition is followed by the script-exhausted
        # failure transition to FAILED.
        assert len(transitions) == 2
        assert transitions[0].state_before is ControllerState.REPRODUCE
        assert transitions[0].state_after is ControllerState.UNDERSTAND
        assert transitions[0].transition_reason == "proceed"
        assert transitions[1].state_after is ControllerState.FAILED

    def test_done_and_failed_transitions_are_terminal(self):
        observer = RecordingObserver()
        result = DeterministicController(
            ToolRegistry(),
            scripted((TransitionDirective(ControllerState.DONE, "resolved"),),
                     states=[ControllerState.VALIDATE]),
            observer=observer,
        ).run(snapshot(ControllerState.VALIDATE))
        assert result.stop_reason is ControllerStopReason.DONE
        assert observer.observations[-1].stop_reason == "done"

        failed_observer = RecordingObserver()
        failed_result = DeterministicController(
            ToolRegistry(),
            scripted((TransitionDirective(ControllerState.FAILED, "gave up"),)),
            observer=failed_observer,
        ).run(snapshot())
        assert failed_result.stop_reason is ControllerStopReason.FAILED
        assert failed_observer.observations[-1].stop_reason == "failed"

    @pytest.mark.parametrize(
        ("scenario", "expected_stop"),
        [
            ("script_exhausted", ControllerStopReason.MODEL_SCRIPT_EXHAUSTED),
            ("script_mismatch", ControllerStopReason.MODEL_SCRIPT_MISMATCH),
            ("model_error", ControllerStopReason.MODEL_ERROR),
            ("model_call_limit", ControllerStopReason.MODEL_CALL_LIMIT),
            ("controller_error", ControllerStopReason.CONTROLLER_ERROR),
        ],
    )
    def test_terminal_categories_observable(self, scenario, expected_stop, monkeypatch):
        if scenario == "script_exhausted":
            registry, adapter = registry_for(ActionName.RUN_TESTS), scripted(
                (ActionDirective(ActionName.RUN_TESTS, {}),)
            )
            config = ControllerRunConfig(max_model_calls=4)
            initial = snapshot()
        elif scenario == "script_mismatch":
            registry, adapter = ToolRegistry(), scripted(
                (TransitionDirective(ControllerState.UNDERSTAND, "proceed"),),
                states=[ControllerState.REPRODUCE],
            )
            config = ControllerRunConfig(max_model_calls=4)
            initial = snapshot(ControllerState.UNDERSTAND)
        elif scenario == "model_error":
            class FailingAdapter:
                model_name = "failing"
                def next_directive(self, call_snapshot):
                    raise ModelAdapterError("model failed")
            registry, adapter = ToolRegistry(), FailingAdapter()
            config = ControllerRunConfig(max_model_calls=4)
            initial = snapshot()
        elif scenario == "model_call_limit":
            registry, adapter = ToolRegistry(), scripted(
                (TransitionDirective(ControllerState.UNDERSTAND, "proceed"),)
            )
            config = ControllerRunConfig(max_model_calls=1)
            initial = snapshot()
        else:
            registry, adapter = registry_for(ActionName.RUN_TESTS), scripted(
                (ActionDirective(ActionName.RUN_TESTS, {}),)
            )
            config = ControllerRunConfig(max_model_calls=4)
            initial = snapshot()

        def raising_dispatch(*args, **kwargs):
            raise RuntimeError("dispatch failed")

        if scenario == "controller_error":
            monkeypatch.setattr(
                controller_module.ToolRegistry, "dispatch", raising_dispatch
            )
        observer = RecordingObserver()
        result = DeterministicController(
            registry, adapter, config, observer=observer
        ).run(initial)
        assert result.stop_reason is expected_stop
        terminals = [
            obs for obs in observer.observations
            if obs.kind is ControllerObservationKind.TERMINAL
        ]
        assert len(terminals) == 1
        assert terminals[0].stop_reason == expected_stop.value
        assert observer.observations[-1] is terminals[0]
        if scenario == "controller_error":
            tool_completed = [
                obs for obs in observer.observations
                if obs.kind is ControllerObservationKind.TOOL_COMPLETED
            ]
            assert len(tool_completed) == 1
            assert tool_completed[0].observation_status is ObservationStatus.ERROR

    def test_model_error_request_boundary_observable(self):
        class FailingAdapter:
            model_name = "failing"
            def next_directive(self, call_snapshot):
                raise ModelAdapterError("model failed")
        observer = RecordingObserver()
        DeterministicController(
            ToolRegistry(), FailingAdapter(), observer=observer,
        ).run(snapshot())
        completed = [
            obs for obs in observer.observations
            if obs.kind is ControllerObservationKind.MODEL_REQUEST_COMPLETED
        ]
        assert len(completed) == 1
        assert completed[0].request_status == "error"

    def test_early_done_and_failed_paths(self):
        done_observer = RecordingObserver()
        done_result = DeterministicController(
            ToolRegistry(), scripted((ActionDirective(ActionName.RUN_TESTS, {}),)),
            observer=done_observer,
        ).run(snapshot(ControllerState.DONE))
        assert done_result.steps == ()
        assert done_result.stop_reason is ControllerStopReason.DONE
        assert [obs.kind for obs in done_observer.observations] == [
            ControllerObservationKind.RUN_STARTED,
            ControllerObservationKind.TERMINAL,
        ]
        assert done_observer.observations[-1].stop_reason == "done"

        failed_observer = RecordingObserver()
        failed_result = DeterministicController(
            ToolRegistry(), scripted((ActionDirective(ActionName.RUN_TESTS, {}),)),
            observer=failed_observer,
        ).run(snapshot(ControllerState.FAILED))
        assert failed_result.steps == ()
        assert failed_result.stop_reason is ControllerStopReason.FAILED
        assert [obs.kind for obs in failed_observer.observations] == [
            ControllerObservationKind.RUN_STARTED,
            ControllerObservationKind.TERMINAL,
        ]
        assert failed_observer.observations[-1].stop_reason == "failed"


class TestObserverSafety:
    def test_observation_is_immutable(self):
        from dataclasses import FrozenInstanceError

        observer = RecordingObserver()
        run_happy(observer=observer)
        with pytest.raises(FrozenInstanceError):
            observer.observations[0].run_id = "mutated"
        with pytest.raises(ControllerObservationError):
            ControllerObservation(kind="bogus", run_id="run-1", task_id="task-1")

    def test_observer_cannot_mutate_controller_state(self):
        class MutatingObserver:
            def __init__(self):
                self.calls = 0
            def notify(self, observation):
                self.calls += 1
                try:
                    object.__setattr__(observation, "kind", "mutated")
                except Exception:
                    pass
        observer = MutatingObserver()
        observed = run_happy(observer=observer)
        plain = run_happy()
        assert observed == plain

    def test_observer_exception_does_not_alter_result(self):
        observer = RecordingObserver(raise_exc=RuntimeError("observer exploded"))
        observed = run_happy(observer=observer)
        plain = run_happy()
        assert observed == plain
        assert observed.stop_reason is ControllerStopReason.DONE

    def test_observer_exception_on_failure_path_does_not_alter_result(self):
        observer = RecordingObserver(raise_exc=RuntimeError("observer exploded"))
        plain = DeterministicController(
            HAPPY_REGISTRY,
            scripted((ActionDirective(ActionName.APPLY_PATCH, {}),)),
        ).run(snapshot())
        observed = DeterministicController(
            HAPPY_REGISTRY,
            scripted((ActionDirective(ActionName.APPLY_PATCH, {}),)),
            observer=observer,
        ).run(snapshot())
        assert observed == plain
        assert observed.stop_reason is ControllerStopReason.DIRECTIVE_REJECTED

    @pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
    def test_base_exception_not_swallowed(self, exc_type):
        observer = RecordingObserver(raise_exc=exc_type("stop"))
        controller = DeterministicController(
            HAPPY_REGISTRY, ScriptedModelAdapter(tuple(HAPPY_STEPS)), observer=observer
        )
        with pytest.raises(exc_type):
            controller.run(snapshot())


class TestObserverValidation:
    def test_constructor_rejects_missing_or_non_callable_notify(self):
        with pytest.raises(ControllerInputError):
            DeterministicController(
                HAPPY_REGISTRY, ScriptedModelAdapter(tuple(HAPPY_STEPS)),
                observer=object(),
            )
        with pytest.raises(ControllerInputError):
            DeterministicController(
                HAPPY_REGISTRY, ScriptedModelAdapter(tuple(HAPPY_STEPS)),
                observer=type("NoNotify", (), {})(),
            )

    def test_custom_observer_instances_are_accepted(self):
        controller = DeterministicController(
            HAPPY_REGISTRY, ScriptedModelAdapter(tuple(HAPPY_STEPS)),
            observer=RecordingObserver(),
        )
        assert controller._canonical_observer is not None
        assert controller.run(snapshot()).final_state is ControllerState.DONE
