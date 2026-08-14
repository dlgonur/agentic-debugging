import pytest

from agentic_debugger.agent.controller import (
    ControllerRunConfig,
    ControllerStopReason,
    DeterministicController,
)
from agentic_debugger.agent.controller_policy import (
    ActionName,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisLedger,
)
from agentic_debugger.agent.model_adapter import (
    ActionDirective,
    ControllerSnapshot,
    ModelDirectiveKind,
    ScriptedModelAdapter,
    ScriptedModelStep,
    TransitionDirective,
)
from agentic_debugger.agent.observer import ControllerObservationKind
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import ToolRegistry, ToolResult, ToolSpec
from agentic_debugger.cancellation import CancellationError, CancellationReason
from agentic_debugger.events.schema import ObservationStatus

LIMITS = ControllerBudgetLimits(2, 3, 2, max_active_hypotheses=2, max_source_observations=3)


def snapshot(state=ControllerState.REPRODUCE, index=0, *, limits=LIMITS):
    return ControllerSnapshot(
        "run-1", "task-1", state, index, limits,
        ControllerBudgetState(), HypothesisLedger(), None,
    )


def scripted(directives):
    return ScriptedModelAdapter(tuple(
        ScriptedModelStep(ControllerState.REPRODUCE, directive)
        for directive in directives
    ))


class RecordingObserver:
    def __init__(self):
        self.kinds = []

    def notify(self, observation):
        self.kinds.append(observation.kind)


def make_tool_handler_raising():
    def handler(action, arguments):
        raise CancellationError(CancellationReason.CANCELLED)

    return handler


def two_action_sequence():
    return [
        ActionDirective(ActionName.RUN_TESTS, {}),
        ActionDirective(ActionName.RUN_TESTS, {}),
    ]


class TestControllerCancellationSeam:
    def test_no_token_parity(self):
        """No cancel_check vs a never-firing check produce identical runs."""
        results = []
        for cancel_check in (None, lambda: None):
            observer = RecordingObserver()
            controller = DeterministicController(
                ToolRegistry(), scripted(two_action_sequence()),
                observer=observer,
            )
            result = controller.run(snapshot(), cancel_check=cancel_check)
            results.append(result)
        first, second = results
        assert first.stop_reason == second.stop_reason
        assert first.final_state == second.final_state
        assert first.model_calls == second.model_calls
        assert len(first.steps) == len(second.steps)
        assert first.to_mapping() == second.to_mapping() if hasattr(first, "to_mapping") else True
        assert first.run_id == second.run_id
        assert first.task_id == second.task_id

    def test_cancel_check_must_be_callable(self):
        controller = DeterministicController(ToolRegistry(), scripted(two_action_sequence()))
        with pytest.raises(Exception):
            controller.run(snapshot(), cancel_check="nope")

    def test_checkpoints_fire_in_expected_positions(self):
        """A two-step run fires exactly 10 checks: loop top, before model
        call, after model call, before dispatch, per iteration."""
        count = {"n": 0}

        def check():
            count["n"] += 1

        controller = DeterministicController(
            ToolRegistry(), scripted(two_action_sequence())
        )
        result = controller.run(snapshot(), cancel_check=check)
        assert result.stop_reason is ControllerStopReason.MODEL_SCRIPT_EXHAUSTED
        assert count["n"] == 10

    def test_cancellation_at_loop_top(self):
        def check():
            raise CancellationError(CancellationReason.CANCELLED)

        controller = DeterministicController(
            ToolRegistry(), scripted(two_action_sequence())
        )
        with pytest.raises(CancellationError):
            controller.run(snapshot(), cancel_check=check)

    def test_cancellation_before_model_call_emits_no_request(self):
        count = {"n": 0}

        def check():
            count["n"] += 1
            if count["n"] == 2:  # loop top passed, before model call
                raise CancellationError(CancellationReason.CANCELLED)

        observer = RecordingObserver()
        controller = DeterministicController(
            ToolRegistry(), scripted(two_action_sequence()), observer=observer
        )
        with pytest.raises(CancellationError):
            controller.run(snapshot(), cancel_check=check)
        assert count["n"] == 2
        assert ControllerObservationKind.MODEL_REQUEST_STARTED not in observer.kinds

    def test_cancellation_after_model_call_emits_request_started_only(self):
        count = {"n": 0}

        def check():
            count["n"] += 1
            if count["n"] == 3:  # after the model call, before completion emit
                raise CancellationError(CancellationReason.CANCELLED)

        observer = RecordingObserver()
        controller = DeterministicController(
            ToolRegistry(), scripted(two_action_sequence()), observer=observer
        )
        with pytest.raises(CancellationError):
            controller.run(snapshot(), cancel_check=check)
        assert count["n"] == 3
        assert ControllerObservationKind.MODEL_REQUEST_STARTED in observer.kinds
        assert ControllerObservationKind.MODEL_REQUEST_COMPLETED not in observer.kinds

    def test_cancellation_before_tool_dispatch_emits_no_tool_started(self):
        count = {"n": 0}

        def check():
            count["n"] += 1
            if count["n"] == 4:  # before the ACTION dispatch block
                raise CancellationError(CancellationReason.CANCELLED)

        observer = RecordingObserver()
        controller = DeterministicController(
            ToolRegistry(), scripted(two_action_sequence()), observer=observer
        )
        with pytest.raises(CancellationError):
            controller.run(snapshot(), cancel_check=check)
        assert count["n"] == 4
        assert ControllerObservationKind.MODEL_REQUEST_COMPLETED in observer.kinds
        assert ControllerObservationKind.TOOL_STARTED not in observer.kinds

    def test_cancellation_is_never_converted_to_a_scientific_stop_reason(self):
        """A cancelled run raises; it cannot become MODEL_ERROR, a policy
        rejection, or a budget outcome."""
        count = {"n": 0}

        def check():
            count["n"] += 1
            if count["n"] >= 3:
                raise CancellationError(CancellationReason.TIMED_OUT)

        controller = DeterministicController(
            ToolRegistry(), scripted(two_action_sequence())
        )
        with pytest.raises(CancellationError) as raised:
            controller.run(snapshot(), cancel_check=check)
        assert raised.value.reason is CancellationReason.TIMED_OUT

    def test_model_adapter_cancellation_propagates_not_model_error(self):
        class CancellingAdapter:
            def next_directive(self, call_snapshot):
                raise CancellationError(CancellationReason.CANCELLED)

        controller = DeterministicController(ToolRegistry(), CancellingAdapter())
        with pytest.raises(CancellationError):
            controller.run(snapshot())

    def test_tool_handler_cancellation_propagates_not_controller_error(self):
        spec = ToolSpec(
            ActionName.RUN_TESTS,
            lambda arguments: arguments,
            make_tool_handler_raising(),
        )
        registry = ToolRegistry((spec,))
        controller = DeterministicController(
            registry, scripted(two_action_sequence())
        )
        with pytest.raises(CancellationError):
            controller.run(snapshot())

    def test_cancel_during_transition_path_propagates(self):
        # A transition directive reaches the loop-top checkpoint of the next
        # iteration; cancellation there must propagate cleanly.
        count = {"n": 0}

        def check():
            count["n"] += 1
            if count["n"] == 2:
                raise CancellationError(CancellationReason.CANCELLED)

        controller = DeterministicController(
            ToolRegistry(),
            scripted((TransitionDirective(ControllerState.DONE, "done"),)),
        )
        with pytest.raises(CancellationError):
            controller.run(snapshot(), cancel_check=check)

    def test_observer_semantics_unchanged_on_non_cancelled_run(self):
        observer = RecordingObserver()
        controller = DeterministicController(
            ToolRegistry(), scripted(two_action_sequence()), observer=observer
        )
        controller.run(snapshot(), cancel_check=lambda: None)
        assert observer.kinds[0] is ControllerObservationKind.RUN_STARTED
        assert observer.kinds[-1] is ControllerObservationKind.TERMINAL
        assert observer.kinds.count(ControllerObservationKind.TOOL_STARTED) == 2
