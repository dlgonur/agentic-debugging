import pytest

from agentic_debugger.agent.controller import (
    DEFAULT_MAX_MODEL_CALLS,
    MAX_CONTROLLER_MODEL_CALLS,
    MAX_CONTROLLER_MODEL_CALL_INDEX,
    ControllerInvariantError,
    ControllerInputError,
    ControllerRunConfig,
    ControllerStepResult,
    ControllerStopReason,
    DeterministicController,
)
from agentic_debugger.agent.controller_policy import (
    ActionName,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisConfidence,
    HypothesisLedger,
    RootCauseHypothesis,
    HypothesisStatus,
)
from agentic_debugger.agent.model_adapter import (
    ActionDirective,
    AddHypothesisDirective,
    ControllerSnapshot,
    ModelDirectiveKind,
    ModelAdapterError,
    ModelScriptExhaustedError,
    ModelScriptMismatchError,
    ReviseHypothesisDirective,
    ScriptedModelAdapter,
    ScriptedModelStep,
    SetHypothesisStatusDirective,
    TransitionDirective,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.agent.tool_registry import (
    ToolDispatchReason,
    ToolExecutionError,
    ToolRegistry,
    ToolRejectedError,
    ToolResult,
    ToolSpec,
    ToolTimeoutError,
)
from agentic_debugger.events.schema import Action, Observation, ObservationStatus


LIMITS = ControllerBudgetLimits(2, 3, 2, max_active_hypotheses=2, max_source_observations=3)


def snapshot(state=ControllerState.REPRODUCE, index=0, *, limits=LIMITS,
             budget=None, ledger=None, last=None):
    return ControllerSnapshot(
        "run-1", "task-1", state, index, limits,
        budget or ControllerBudgetState(), ledger or HypothesisLedger(), last,
    )


def registry_for(*names, counters=None):
    counters = counters if counters is not None else {"validator": 0, "handler": 0}

    def validator(arguments):
        counters["validator"] += 1
        return arguments

    def handler(action, arguments):
        counters["handler"] += 1
        return ToolResult(ObservationStatus.OK, {"received": arguments}, "ok")

    return ToolRegistry(tuple(
        ToolSpec(name, validator, handler) for name in names
    )), counters


def scripted(directives, states=None):
    states = states or [ControllerState.REPRODUCE] * len(directives)
    return ScriptedModelAdapter(tuple(
        ScriptedModelStep(state, directive)
        for state, directive in zip(states, directives)
    ))


def test_constants_config_and_stop_reasons_are_exact():
    assert DEFAULT_MAX_MODEL_CALLS == 64
    assert MAX_CONTROLLER_MODEL_CALLS == 10_000
    assert MAX_CONTROLLER_MODEL_CALL_INDEX == 999_999_999
    assert [reason.value for reason in ControllerStopReason] == [
        "done", "failed", "model_script_exhausted", "model_script_mismatch",
        "model_error", "directive_rejected", "budget_exhausted",
        "model_call_limit", "controller_error",
    ]
    assert len(ControllerStopReason.__members__) == 9
    assert ControllerRunConfig() == ControllerRunConfig(DEFAULT_MAX_MODEL_CALLS)
    ControllerRunConfig(1)
    ControllerRunConfig(MAX_CONTROLLER_MODEL_CALLS)
    for value in (0, -1, MAX_CONTROLLER_MODEL_CALLS + 1, True, False):
        with pytest.raises(ControllerInputError):
            ControllerRunConfig(value)


def test_default_config_records_are_isolated_and_canonical_limits_are_retained():
    directive = TransitionDirective(ControllerState.UNDERSTAND, "construct")
    first = DeterministicController(ToolRegistry(), RecordingAdapter([directive]))
    second = DeterministicController(ToolRegistry(), RecordingAdapter([directive]))
    assert first.config is not second.config
    assert first.config.max_model_calls == DEFAULT_MAX_MODEL_CALLS
    assert second.config.max_model_calls == DEFAULT_MAX_MODEL_CALLS

    object.__setattr__(first.config, "max_model_calls", 1)
    third = DeterministicController(ToolRegistry(), RecordingAdapter([directive]))
    assert second.config.max_model_calls == DEFAULT_MAX_MODEL_CALLS
    assert third.config.max_model_calls == DEFAULT_MAX_MODEL_CALLS
    assert first._canonical_max_model_calls == DEFAULT_MAX_MODEL_CALLS
    assert second._canonical_max_model_calls == DEFAULT_MAX_MODEL_CALLS
    assert third._canonical_max_model_calls == DEFAULT_MAX_MODEL_CALLS


def test_controller_construction_is_pure_and_canonicalizes_registry():
    counters = {"validator": 0, "handler": 0}
    registry, _ = registry_for(ActionName.RUN_TESTS, counters=counters)
    adapter = scripted((ActionDirective(ActionName.RUN_TESTS, {}),))
    controller = DeterministicController(registry, adapter)
    assert controller._canonical_registry is not registry
    assert controller._canonical_registry.specs[0] is not registry.specs[0]
    assert counters == {"validator": 0, "handler": 0}
    original_spec = registry.specs[0]
    object.__setattr__(registry, "specs", ())
    object.__setattr__(original_spec, "handler", lambda *_: (_ for _ in ()).throw(AssertionError()))
    result = controller.run(snapshot())
    assert result.steps[0].observation.observation_id == "observation-000000000"
    assert counters == {"validator": 1, "handler": 1}


class RecordingAdapter:
    def __init__(self, directives):
        self.directives = list(directives)
        self.snapshots = []
        self.calls = 0

    def next_directive(self, snapshot):
        self.calls += 1
        self.snapshots.append(snapshot)
        return self.directives.pop(0)


def test_static_model_boundary_ignores_instance_shadow_and_progresses_snapshots():
    registry, counters = registry_for(ActionName.RUN_TESTS)
    adapter = RecordingAdapter([
        ActionDirective(ActionName.RUN_TESTS, {"nested": {"x": [1]}}),
        TransitionDirective(ControllerState.UNDERSTAND, "continue"),
    ])
    shadow_calls = []
    object.__setattr__(adapter, "next_directive", lambda *_: shadow_calls.append("shadow"))
    result = DeterministicController(registry, adapter, ControllerRunConfig(2)).run(snapshot())
    assert shadow_calls == []
    assert adapter.calls == 2
    assert result.stop_reason is ControllerStopReason.MODEL_CALL_LIMIT
    assert [snap.model_call_index for snap in adapter.snapshots] == [0, 1]
    assert adapter.snapshots[1].state is ControllerState.REPRODUCE
    assert adapter.snapshots[1].last_observation.observation_id == "observation-000000000"
    assert adapter.snapshots[1].budget_state.test_runs == 1
    assert counters == {"validator": 1, "handler": 1}
    assert result.steps[0].action.action_id == "action-000000000"
    assert result.steps[0].observation.observation_id == "observation-000000000"
    assert result.steps[1].model_call_index == 1


def test_two_action_trajectory_has_deterministic_ids_and_reason_based_budget():
    registry, counters = registry_for(ActionName.RUN_TESTS, ActionName.GET_FAILURE_TRACE)
    adapter = scripted(
        [
            ActionDirective(ActionName.RUN_TESTS, {}),
            ActionDirective(ActionName.GET_FAILURE_TRACE, {}),
            TransitionDirective(ControllerState.UNDERSTAND, "done with reproduce"),
        ],
        [ControllerState.REPRODUCE] * 3,
    )
    result = DeterministicController(registry, adapter, ControllerRunConfig(3)).run(snapshot())
    assert result.stop_reason is ControllerStopReason.MODEL_CALL_LIMIT
    assert [step.model_call_index for step in result.steps] == [0, 1, 2]
    assert result.steps[0].action.action_id == "action-000000000"
    assert result.steps[1].action.action_id == "action-000000001"
    assert result.steps[1].observation.observation_id == "observation-000000001"
    assert result.budget_state.test_runs == 1
    assert counters == {"validator": 2, "handler": 2}


def test_forbidden_action_and_budget_exhaustion_happen_before_registry():
    registry, counters = registry_for(ActionName.RUN_TESTS, ActionName.SEARCH_CODE)
    forbidden = scripted((ActionDirective(ActionName.RUN_TESTS, {}),), [ControllerState.UNDERSTAND])
    result = DeterministicController(registry, forbidden, ControllerRunConfig(1)).run(
        snapshot(ControllerState.UNDERSTAND)
    )
    assert result.stop_reason is ControllerStopReason.DIRECTIVE_REJECTED
    assert result.steps[0].action is None
    assert counters == {"validator": 0, "handler": 0}

    counters = {"validator": 0, "handler": 0}
    registry, _ = registry_for(ActionName.RUN_TESTS, counters=counters)
    adapter = scripted((
        ActionDirective(ActionName.RUN_TESTS, {}),
        ActionDirective(ActionName.RUN_TESTS, {}),
    ))
    exhausted = DeterministicController(registry, adapter, ControllerRunConfig(2)).run(
        snapshot(budget=ControllerBudgetState(test_runs=LIMITS.max_test_runs))
    )
    assert exhausted.stop_reason is ControllerStopReason.BUDGET_EXHAUSTED
    assert exhausted.steps[-1].action is None
    assert exhausted.budget_state.test_runs == LIMITS.max_test_runs
    assert counters == {"validator": 0, "handler": 0}


def test_observation_error_does_not_transition_and_can_be_followed_by_transition():
    counters = {"validator": 0, "handler": 0}

    def validator(arguments):
        counters["validator"] += 1
        return arguments

    def handler(action, arguments):
        counters["handler"] += 1
        raise ToolRejectedError("secret handler message")

    registry = ToolRegistry((ToolSpec(ActionName.RUN_TESTS, validator, handler),))
    adapter = scripted((
        ActionDirective(ActionName.RUN_TESTS, {}),
        TransitionDirective(ControllerState.UNDERSTAND, "observe error"),
    ))
    result = DeterministicController(registry, adapter, ControllerRunConfig(2)).run(snapshot())
    assert result.stop_reason is ControllerStopReason.MODEL_CALL_LIMIT
    assert result.steps[0].observation.status is ObservationStatus.REJECTED
    assert result.steps[0].state_after is ControllerState.REPRODUCE
    assert result.steps[0].observation.payload == {"dispatch_reason": "tool_rejected"}
    assert result.budget_state.test_runs == 1
    assert counters == {"validator": 1, "handler": 1}


def test_hypothesis_lifecycle_is_controller_owned_and_state_gated():
    add = AddHypothesisDirective("h-1", "first", HypothesisConfidence.LOW)
    revise = ReviseHypothesisDirective("h-1", "revised", HypothesisConfidence.HIGH, (), True)
    status = SetHypothesisStatusDirective("h-1", HypothesisStatus.SUPPORTED)
    adapter = scripted(
        [add, revise, status, TransitionDirective(ControllerState.RUNTIME_EVIDENCE, "runtime")],
        [ControllerState.UNDERSTAND, ControllerState.UNDERSTAND,
         ControllerState.UNDERSTAND, ControllerState.UNDERSTAND],
    )
    registry = ToolRegistry()
    result = DeterministicController(registry, adapter, ControllerRunConfig(4)).run(snapshot(ControllerState.UNDERSTAND))
    assert result.stop_reason is ControllerStopReason.MODEL_CALL_LIMIT
    record = result.hypotheses.hypotheses[0]
    assert record.hypothesis_id == "h-1"
    assert record.revision == 2
    assert record.status is HypothesisStatus.SUPPORTED
    assert result.steps[0].hypotheses_before is not result.steps[0].hypotheses_after
    assert result.steps[3].state_after is ControllerState.RUNTIME_EVIDENCE


def test_duplicate_hypothesis_and_illegal_transition_are_rejected_without_partial_state():
    ledger = HypothesisLedger().add(
        LIMITS, hypothesis_id="h-1", statement="existing", confidence=HypothesisConfidence.LOW
    )
    duplicate = scripted((AddHypothesisDirective("h-1", "duplicate", HypothesisConfidence.HIGH),), [ControllerState.UNDERSTAND])
    result = DeterministicController(ToolRegistry(), duplicate).run(
        snapshot(ControllerState.UNDERSTAND, ledger=ledger)
    )
    assert result.stop_reason is ControllerStopReason.DIRECTIVE_REJECTED
    assert result.hypotheses == ledger

    illegal = scripted((TransitionDirective(ControllerState.DONE, "not legal"),))
    result = DeterministicController(ToolRegistry(), illegal).run(snapshot())
    assert result.stop_reason is ControllerStopReason.DIRECTIVE_REJECTED
    assert result.final_state is ControllerState.FAILED
    assert result.steps[0].transition_reason == "not legal"


@pytest.mark.parametrize("error,reason", [
    (ModelScriptExhaustedError, ControllerStopReason.MODEL_SCRIPT_EXHAUSTED),
    (ModelScriptMismatchError, ControllerStopReason.MODEL_SCRIPT_MISMATCH),
    (ModelAdapterError, ControllerStopReason.MODEL_ERROR),
    (RuntimeError, ControllerStopReason.MODEL_ERROR),
])
def test_model_failures_are_translated_without_exception_text(error, reason):
    class FailingAdapter:
        def next_directive(self, snapshot):
            raise error("SECRET-EXCEPTION-TEXT")

    controller = DeterministicController(ToolRegistry(), FailingAdapter())
    result = controller.run(snapshot())
    assert result.stop_reason is reason
    assert result.final_state is ControllerState.FAILED
    assert result.model_calls == 1
    assert len(result.steps) == 1
    assert "SECRET" not in repr(result)
    assert "Exception" not in repr(result)


def test_model_call_limit_and_initial_terminal_snapshots_do_not_call_model_or_registry():
    class NeverCalled:
        def next_directive(self, snapshot):
            raise AssertionError("called")

    registry = ToolRegistry()
    controller = DeterministicController(registry, NeverCalled(), ControllerRunConfig(1))
    for state, stop in ((ControllerState.DONE, ControllerStopReason.DONE),
                        (ControllerState.FAILED, ControllerStopReason.FAILED)):
        result = controller.run(snapshot(state))
        assert result.stop_reason is stop
        assert result.model_calls == 0
        assert result.steps == ()


def test_absolute_index_overflow_and_snapshot_canonicalization():
    adapter = scripted((TransitionDirective(ControllerState.UNDERSTAND, "next"),))
    controller = DeterministicController(ToolRegistry(), adapter, ControllerRunConfig(2))
    with pytest.raises(ControllerInputError):
        controller.run(snapshot(index=MAX_CONTROLLER_MODEL_CALL_INDEX - 1))
    forged = snapshot()
    object.__setattr__(forged, "model_call_index", True)
    with pytest.raises(ControllerInputError):
        controller.run(forged)


def test_returned_observation_payload_is_detached_from_later_runs():
    registry, _ = registry_for(ActionName.RUN_TESTS)
    adapter = scripted((ActionDirective(ActionName.RUN_TESTS, {}),))
    controller = DeterministicController(registry, adapter, ControllerRunConfig(1))
    first = controller.run(snapshot())
    first.last_observation.payload["mutated"] = True
    second = controller.run(snapshot())
    assert "mutated" not in second.last_observation.payload



def _last_observation():
    return Observation(
        "observation-prior", "action-prior", "run-1", "task-1",
        ActionName.RUN_TESTS.value, ObservationStatus.OK,
        {"dispatch_reason": "ok", "value": "stable"}, "prior", False,
    )


class _SnapshotMutator:
    def __init__(self, directive, *, mutate=None):
        self.directive = directive
        self.mutate = mutate
        self.snapshots = []

    def next_directive(self, snapshot):
        self.snapshots.append(snapshot)
        if self.mutate is not None:
            self.mutate(snapshot)
        return self.directive


def test_model_snapshot_records_are_fresh_and_mutations_cannot_change_local_state():
    limits = ControllerBudgetLimits(1, 1, 0, max_active_hypotheses=1)
    record = HypothesisLedger().add(
        limits, hypothesis_id="h-1", statement="stable", confidence=HypothesisConfidence.LOW
    )
    initial = snapshot(
        ControllerState.REPRODUCE, limits=limits, ledger=record, last=_last_observation()
    )

    def mutate(model_snapshot):
        object.__setattr__(model_snapshot, "state", ControllerState.DONE)
        object.__setattr__(model_snapshot, "model_call_index", 999)
        object.__setattr__(model_snapshot, "run_id", "forged-run")
        object.__setattr__(model_snapshot, "task_id", "forged-task")
        object.__setattr__(model_snapshot.budget_limits, "max_test_runs", 999)
        object.__setattr__(model_snapshot.budget_state, "test_runs", 0)
        model_record = model_snapshot.hypotheses.hypotheses[0]
        object.__setattr__(model_snapshot.hypotheses, "hypotheses", ())
        object.__setattr__(model_record, "statement", "forged")
        object.__setattr__(model_snapshot.hypotheses, "hypotheses", (model_record,))
        object.__setattr__(model_snapshot.last_observation, "payload", {"dispatch_reason": "tool_error"})

    adapter = _SnapshotMutator(
        TransitionDirective(ControllerState.UNDERSTAND, "actual state"), mutate=mutate
    )
    controller = DeterministicController(ToolRegistry(), adapter, ControllerRunConfig(1))
    result = controller.run(initial)
    model_snapshot = adapter.snapshots[0]
    assert model_snapshot.budget_limits is not initial.budget_limits
    assert model_snapshot.budget_state is not initial.budget_state
    assert model_snapshot.hypotheses is not initial.hypotheses
    assert model_snapshot.last_observation is not initial.last_observation
    assert model_snapshot.last_observation.payload is not initial.last_observation.payload
    assert result.steps[0].state_before is ControllerState.REPRODUCE
    assert result.steps[0].state_after is ControllerState.UNDERSTAND
    assert result.steps[0].model_call_index == 0
    assert result.budget_state == ControllerBudgetState()
    assert result.hypotheses == record
    assert result.last_observation.payload == {"dispatch_reason": "ok", "value": "stable"}


class _HostileCounter:
    def __init__(self):
        self.hooks = []

    def _trip(self, name):
        self.hooks.append(name)
        raise AssertionError(name)

    __add__ = lambda self, other: self._trip("add")
    __radd__ = lambda self, other: self._trip("radd")
    __sub__ = lambda self, other: self._trip("sub")
    __rsub__ = lambda self, other: self._trip("rsub")
    __lt__ = lambda self, other: self._trip("lt")
    __le__ = lambda self, other: self._trip("le")
    __gt__ = lambda self, other: self._trip("gt")
    __ge__ = lambda self, other: self._trip("ge")
    __eq__ = lambda self, other: self._trip("eq")
    __int__ = lambda self: self._trip("int")
    __index__ = lambda self: self._trip("index")
    __repr__ = lambda self: self._trip("repr")
    __str__ = lambda self: self._trip("str")


@pytest.mark.parametrize("counter_field", [
    "patch_attempts", "test_runs", "pdb_observations", "source_observations",
])
@pytest.mark.parametrize("counter_value", [0, -1, 2, True, _HostileCounter()])
def test_model_budget_counter_mutation_cannot_reopen_exhaustion(counter_field, counter_value):
    limits = ControllerBudgetLimits(1, 1, 0)
    calls = {"validator": 0, "handler": 0}

    def validator(arguments):
        calls["validator"] += 1
        return arguments

    def handler(action, arguments):
        calls["handler"] += 1
        return ToolResult(ObservationStatus.OK, {}, "ok")

    def mutate(model_snapshot):
        object.__setattr__(model_snapshot.budget_state, counter_field, counter_value)

    adapter = _SnapshotMutator(ActionDirective(ActionName.RUN_TESTS, {}), mutate=mutate)
    result = DeterministicController(
        ToolRegistry((ToolSpec(ActionName.RUN_TESTS, validator, handler),)), adapter
    ).run(snapshot(limits=limits, budget=ControllerBudgetState(test_runs=1)))
    assert result.stop_reason is ControllerStopReason.BUDGET_EXHAUSTED
    assert result.budget_state == ControllerBudgetState(test_runs=1)
    assert calls == {"validator": 0, "handler": 0}
    if isinstance(counter_value, _HostileCounter):
        assert counter_value.hooks == []


def test_model_budget_limit_mutation_cannot_reopen_exhaustion():
    limits = ControllerBudgetLimits(1, 1, 0)
    calls = {"validator": 0, "handler": 0}

    def mutate(model_snapshot):
        object.__setattr__(model_snapshot.budget_limits, "max_test_runs", 999)

    def validator(arguments):
        calls["validator"] += 1
        return arguments

    def handler(action, arguments):
        calls["handler"] += 1
        return ToolResult(ObservationStatus.OK, {}, "ok")

    result = DeterministicController(
        ToolRegistry((ToolSpec(ActionName.RUN_TESTS, validator, handler),)),
        _SnapshotMutator(ActionDirective(ActionName.RUN_TESTS, {}), mutate=mutate),
    ).run(snapshot(limits=limits, budget=ControllerBudgetState(test_runs=1)))
    assert result.stop_reason is ControllerStopReason.BUDGET_EXHAUSTED
    assert result.budget_state == ControllerBudgetState(test_runs=1)
    assert calls == {"validator": 0, "handler": 0}


@pytest.mark.parametrize("field,action,state,counter_field", [
    ("max_patch_attempts", ActionName.APPLY_PATCH, ControllerState.PATCH, "patch_attempts"),
    ("max_test_runs", ActionName.RUN_TESTS, ControllerState.REPRODUCE, "test_runs"),
    ("max_pdb_observations", ActionName.GET_STACK_SUMMARY, ControllerState.RUNTIME_EVIDENCE, "pdb_observations"),
    ("max_source_observations", ActionName.SEARCH_CODE, ControllerState.UNDERSTAND, "source_observations"),
])
def test_every_model_budget_limit_mutation_cannot_change_gate_or_dispatch(
    field, action, state, counter_field,
):
    limits = ControllerBudgetLimits(1, 1, 1, max_active_hypotheses=1, max_source_observations=1)
    budget = ControllerBudgetState(**{counter_field: 1})
    registry, calls = registry_for(action)

    def mutate(model_snapshot):
        object.__setattr__(model_snapshot.budget_limits, field, 999)

    result = DeterministicController(
        registry, _SnapshotMutator(ActionDirective(action, {}), mutate=mutate)
    ).run(snapshot(state, limits=limits, budget=budget))
    assert result.stop_reason is ControllerStopReason.BUDGET_EXHAUSTED
    assert result.budget_state == budget
    assert calls == {"validator": 0, "handler": 0}


def test_model_active_hypothesis_limit_mutation_cannot_bypass_capacity():
    limits = ControllerBudgetLimits(1, 1, 0, max_active_hypotheses=1)
    original = HypothesisLedger().add(
        limits, hypothesis_id="h-1", statement="stable", confidence=HypothesisConfidence.LOW
    )

    def mutate(model_snapshot):
        object.__setattr__(model_snapshot.budget_limits, "max_active_hypotheses", 999)

    result = DeterministicController(
        ToolRegistry(),
        _SnapshotMutator(
            AddHypothesisDirective("h-2", "forged", HypothesisConfidence.HIGH),
            mutate=mutate,
        ),
    ).run(snapshot(ControllerState.UNDERSTAND, limits=limits, ledger=original))
    assert result.stop_reason is ControllerStopReason.DIRECTIVE_REJECTED
    assert result.hypotheses == original
    assert result.hypotheses.hypotheses[0].hypothesis_id == "h-1"


def test_model_ledger_injection_and_record_mutation_cannot_bypass_capacity_or_change_result():
    limits = ControllerBudgetLimits(1, 1, 0, max_active_hypotheses=1)
    original = HypothesisLedger().add(
        limits, hypothesis_id="h-1", statement="stable", confidence=HypothesisConfidence.LOW
    )
    injected = RootCauseHypothesis(
        "h-injected", "injected", HypothesisConfidence.HIGH,
        HypothesisStatus.ACTIVE, (), False, 1,
    )

    def inject(model_snapshot):
        model_record = model_snapshot.hypotheses.hypotheses[0]
        object.__setattr__(model_record, "statement", "forged")
        object.__setattr__(model_snapshot.hypotheses, "hypotheses", (model_record, injected))

    adapter = _SnapshotMutator(
        AddHypothesisDirective("h-2", "new", HypothesisConfidence.HIGH), mutate=inject
    )
    result = DeterministicController(ToolRegistry(), adapter).run(
        snapshot(ControllerState.UNDERSTAND, limits=limits, ledger=original)
    )
    assert result.stop_reason is ControllerStopReason.DIRECTIVE_REJECTED
    assert result.hypotheses == original
    assert len(result.hypotheses.hypotheses) == 1
    assert result.hypotheses.hypotheses[0].statement == "stable"


@pytest.mark.parametrize("field,value", [
    ("hypothesis_id", "forged-id"),
    ("statement", "forged statement"),
    ("status", HypothesisStatus.DISCARDED),
    ("revision", 99),
])
def test_model_hypothesis_record_mutation_cannot_forge_controller_state(field, value):
    limits = ControllerBudgetLimits(1, 1, 0, max_active_hypotheses=1)
    original = HypothesisLedger().add(
        limits, hypothesis_id="h-1", statement="stable", confidence=HypothesisConfidence.LOW
    )

    def mutate(model_snapshot):
        model_record = model_snapshot.hypotheses.hypotheses[0]
        object.__setattr__(model_record, field, value)

    result = DeterministicController(
        ToolRegistry(),
        _SnapshotMutator(
            TransitionDirective(ControllerState.RUNTIME_EVIDENCE, "actual"),
            mutate=mutate,
        ),
    ).run(snapshot(ControllerState.UNDERSTAND, limits=limits, ledger=original))
    assert result.stop_reason is ControllerStopReason.MODEL_CALL_LIMIT
    assert result.hypotheses == original
    assert result.hypotheses.hypotheses[0].hypothesis_id == "h-1"
    assert result.hypotheses.hypotheses[0].statement == "stable"
    assert result.hypotheses.hypotheses[0].revision == 1
    assert all(
        record.hypothesis_id == "h-1"
        for step in result.steps
        for ledger in (step.hypotheses_before, step.hypotheses_after)
        for record in ledger.hypotheses
    )


def test_model_terminal_hypothesis_mutation_cannot_reopen_controller_record():
    limits = ControllerBudgetLimits(1, 1, 0, max_active_hypotheses=1)
    original = HypothesisLedger().add(
        limits, hypothesis_id="h-1", statement="terminal", confidence=HypothesisConfidence.LOW
    ).transition("h-1", HypothesisStatus.DISCARDED)

    def mutate(model_snapshot):
        object.__setattr__(model_snapshot.hypotheses.hypotheses[0], "status", HypothesisStatus.ACTIVE)

    result = DeterministicController(
        ToolRegistry(),
        _SnapshotMutator(
            ReviseHypothesisDirective("h-1", "reopen", HypothesisConfidence.HIGH, (), False),
            mutate=mutate,
        ),
    ).run(snapshot(ControllerState.UNDERSTAND, limits=limits, ledger=original))
    assert result.stop_reason is ControllerStopReason.DIRECTIVE_REJECTED
    assert result.hypotheses == original
    assert result.hypotheses.hypotheses[0].status is HypothesisStatus.DISCARDED


def test_consecutive_model_calls_receive_fresh_detached_hypothesis_ledgers_and_records():
    limits = ControllerBudgetLimits(1, 1, 0, max_active_hypotheses=1)
    initial_ledger = HypothesisLedger().add(
        limits, hypothesis_id="h-1", statement="stable", confidence=HypothesisConfidence.LOW
    )
    adapter = RecordingAdapter([
        TransitionDirective(ControllerState.RUNTIME_EVIDENCE, "first"),
        TransitionDirective(ControllerState.UNDERSTAND, "second"),
    ])
    result = DeterministicController(ToolRegistry(), adapter, ControllerRunConfig(2)).run(
        snapshot(ControllerState.UNDERSTAND, limits=limits, ledger=initial_ledger)
    )
    assert result.stop_reason is ControllerStopReason.MODEL_CALL_LIMIT
    first_ledger = adapter.snapshots[0].hypotheses
    second_ledger = adapter.snapshots[1].hypotheses
    assert first_ledger is not second_ledger
    assert first_ledger.hypotheses[0] is not second_ledger.hypotheses[0]
    assert first_ledger is not initial_ledger
    assert second_ledger is not initial_ledger
    assert first_ledger.hypotheses[0] is not initial_ledger.hypotheses[0]
    assert second_ledger.hypotheses[0] is not initial_ledger.hypotheses[0]


def test_model_last_observation_mutation_cannot_rewrite_canonical_observation():
    limits = ControllerBudgetLimits(1, 1, 0)

    def mutate(model_snapshot):
        observation = model_snapshot.last_observation
        object.__setattr__(observation, "payload", {"dispatch_reason": "tool_error"})
        object.__setattr__(observation, "status", ObservationStatus.ERROR)
        object.__setattr__(observation, "action_id", "forged-action")
        object.__setattr__(observation, "run_id", "forged-run")
        object.__setattr__(observation, "task_id", "forged-task")
        object.__setattr__(observation, "name", "forged-name")
        object.__setattr__(observation, "summary", "forged-summary")
        object.__setattr__(observation, "truncated", True)

    original = _last_observation()
    result = DeterministicController(
        ToolRegistry(), _SnapshotMutator(ActionDirective(ActionName.RUN_TESTS, {}), mutate=mutate)
    ).run(snapshot(limits=limits, budget=ControllerBudgetState(test_runs=1), last=original))
    assert result.stop_reason is ControllerStopReason.BUDGET_EXHAUSTED
    assert result.last_observation.payload == {"dispatch_reason": "ok", "value": "stable"}
    assert result.last_observation.action_id == "action-prior"
    assert result.last_observation.status is ObservationStatus.OK


def _run_mutating_handler(mutation, error=None):
    limits = ControllerBudgetLimits(1, 1, 0)

    def handler(action, arguments):
        mutation(action)
        if error is not None:
            raise error("handler secret")
        return ToolResult(ObservationStatus.OK, {}, "ok")

    directive = ActionDirective(ActionName.RUN_TESTS, {"nested": {"value": "original"}})
    adapter = scripted((directive,))
    result = DeterministicController(
        ToolRegistry((ToolSpec(ActionName.RUN_TESTS, lambda arguments: arguments, handler),)), adapter
    ).run(snapshot(limits=limits))
    return directive, result


def test_handler_argument_mutation_cannot_change_record_action():
    directive, result = _run_mutating_handler(
        lambda action: action.arguments["nested"].__setitem__("value", "changed")
    )
    assert result.stop_reason is ControllerStopReason.MODEL_SCRIPT_EXHAUSTED
    assert result.steps[0].action.arguments == {"nested": {"value": "original"}}
    assert directive.arguments == {"nested": {"value": "original"}}


@pytest.mark.parametrize("field,value", [
    ("action_id", "forged-action"),
    ("run_id", "forged-run"),
    ("task_id", "forged-task"),
    ("name", "forged-name"),
    ("state", ControllerState.UNDERSTAND),
])
def test_handler_correlation_mutation_returns_controller_error_with_record_action(field, value):
    _, result = _run_mutating_handler(lambda action: object.__setattr__(action, field, value))
    assert result.stop_reason is ControllerStopReason.CONTROLLER_ERROR
    assert result.final_state is ControllerState.FAILED
    assert result.steps[0].action.action_id == "action-000000000"
    assert result.steps[0].action.run_id == "run-1"
    assert result.steps[0].action.task_id == "task-1"
    assert result.steps[0].action.name == ActionName.RUN_TESTS.value
    assert result.steps[0].action.state is ControllerState.REPRODUCE
    assert result.steps[0].observation is None
    assert result.budget_state == ControllerBudgetState()


@pytest.mark.parametrize("error", [ToolRejectedError, ToolTimeoutError, ToolExecutionError, RuntimeError])
def test_handler_correlation_mutation_followed_by_exception_is_bounded(error):
    _, result = _run_mutating_handler(
        lambda action: object.__setattr__(action, "run_id", "forged-run"), error
    )
    assert result.stop_reason is ControllerStopReason.CONTROLLER_ERROR
    assert result.steps[0].action.run_id == "run-1"
    assert result.steps[0].observation is None
    assert result.budget_state == ControllerBudgetState()


class _CustomMapping:
    def __init__(self):
        self.hooks = []

    def __iter__(self):
        self.hooks.append("iter")
        raise AssertionError("iter")

    def __getitem__(self, key):
        self.hooks.append("getitem")
        raise AssertionError("getitem")


@pytest.mark.parametrize("bad_value", [object(), (1, 2), {1, 2}, b"bytes", _CustomMapping()])
def test_handler_invalid_argument_injection_never_enters_record(bad_value):
    _, result = _run_mutating_handler(
        lambda action: object.__setattr__(action, "arguments", {"bad": bad_value})
    )
    assert result.stop_reason is ControllerStopReason.MODEL_SCRIPT_EXHAUSTED
    assert result.steps[0].action.arguments == {"nested": {"value": "original"}}
    assert "bad" not in result.steps[0].action.arguments
    if isinstance(bad_value, _CustomMapping):
        assert bad_value.hooks == []


def test_handler_cyclic_argument_injection_never_enters_record():
    cyclic = []
    cyclic.append(cyclic)
    _, result = _run_mutating_handler(
        lambda action: object.__setattr__(action, "arguments", {"bad": cyclic})
    )
    assert result.stop_reason is ControllerStopReason.MODEL_SCRIPT_EXHAUSTED
    assert result.steps[0].action.arguments == {"nested": {"value": "original"}}


class _QueueAdapter:
    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    def next_directive(self, snapshot):
        self.calls += 1
        return self.values.pop(0)


class _HostileConfigValue:
    def __init__(self):
        self.hooks = []

    def _trip(self, name):
        self.hooks.append(name)
        raise AssertionError(name)

    def __radd__(self, other): return self._trip("radd")
    def __add__(self, other): return self._trip("add")
    def __ge__(self, other): return self._trip("ge")
    def __le__(self, other): return self._trip("le")
    def __index__(self): return self._trip("index")
    def __int__(self): return self._trip("int")
    def __repr__(self): return self._trip("repr")
    def __str__(self): return self._trip("str")


class ValidControllerRunConfigSubclass(ControllerRunConfig):
    pass


@pytest.mark.parametrize("value", [0, -1, MAX_CONTROLLER_MODEL_CALLS + 1, True, False, 1.0, "bad", _HostileConfigValue()])
def test_original_config_mutation_cannot_change_private_call_limit(value):
    config = ControllerRunConfig(2)
    adapter = _QueueAdapter([
        TransitionDirective(ControllerState.UNDERSTAND, "one"),
        TransitionDirective(ControllerState.PATCH, "two"),
    ])
    controller = DeterministicController(ToolRegistry(), adapter, config)
    object.__setattr__(config, "max_model_calls", value)
    result = controller.run(snapshot())
    assert result.model_calls == 2
    assert result.stop_reason is ControllerStopReason.MODEL_CALL_LIMIT
    assert adapter.calls == 2
    assert controller.config is not config
    if isinstance(value, _HostileConfigValue):
        assert value.hooks == []


def test_original_config_class_mutation_cannot_reenter_run_validation():
    config = ControllerRunConfig(2)
    adapter = _QueueAdapter([
        TransitionDirective(ControllerState.UNDERSTAND, "one"),
        TransitionDirective(ControllerState.PATCH, "two"),
    ])
    controller = DeterministicController(ToolRegistry(), adapter, config)
    object.__setattr__(config, "__class__", ValidControllerRunConfigSubclass)
    result = controller.run(snapshot())
    assert result.model_calls == 2
    assert result.stop_reason is ControllerStopReason.MODEL_CALL_LIMIT
    assert adapter.calls == 2


def test_two_controllers_keep_independent_canonical_call_limits():
    first_adapter = _QueueAdapter([TransitionDirective(ControllerState.UNDERSTAND, "one")])
    second_adapter = _QueueAdapter([
        TransitionDirective(ControllerState.UNDERSTAND, "one"),
        TransitionDirective(ControllerState.PATCH, "two"),
    ])
    first = DeterministicController(ToolRegistry(), first_adapter, ControllerRunConfig(1))
    second = DeterministicController(ToolRegistry(), second_adapter, ControllerRunConfig(2))
    assert first.run(snapshot()).model_calls == 1
    assert second.run(snapshot()).model_calls == 2


class _HostileKey:
    def __init__(self):
        self.hooks = []
        self.raise_on_hash = False

    def __hash__(self):
        if self.raise_on_hash:
            self.hooks.append("hash")
            raise AssertionError("hash")
        return 17

    def __eq__(self, other):
        self.hooks.append("eq")
        raise AssertionError("eq")


class _StringKey(str):
    def __new__(cls, value, hooks):
        instance = str.__new__(cls, value)
        instance.hooks = hooks
        return instance

    def __hash__(self):
        if getattr(self, "raise_on_hash", True):
            self.hooks.append("hash")
            raise AssertionError("hash")
        return str.__hash__(self)

    def __eq__(self, other):
        self.hooks.append("eq")
        raise AssertionError("eq")


class _HostileObservationScalar:
    def __init__(self):
        self.hooks = []

    def _trip(self, name):
        self.hooks.append(name)
        raise AssertionError(name)

    __eq__ = lambda self, other: self._trip("eq")
    __bool__ = lambda self: self._trip("bool")
    __hash__ = lambda self: self._trip("hash")
    __repr__ = lambda self: self._trip("repr")
    __str__ = lambda self: self._trip("str")


def _step_with_observation(observation):
    action = Action(
        "action-000000000", "run-1", "task-1", ControllerState.REPRODUCE,
        ActionName.RUN_TESTS.value, {},
    )
    return ControllerStepResult(
        0, ControllerState.REPRODUCE, ControllerState.REPRODUCE,
        ModelDirectiveKind.ACTION, action, observation, None,
        ControllerBudgetState(), ControllerBudgetState(),
        HypothesisLedger(), HypothesisLedger(),
    )


@pytest.mark.parametrize("field", [
    "observation_id", "action_id", "run_id", "task_id", "name", "status",
    "payload", "summary", "truncated",
])
def test_step_result_rejects_hostile_observation_scalars_without_hooks(field):
    hostile = _HostileObservationScalar()
    observation = Observation(
        "observation-000000000", "action-000000000", "run-1", "task-1",
        ActionName.RUN_TESTS.value, ObservationStatus.OK,
        {"dispatch_reason": "ok"}, "summary", False,
    )
    object.__setattr__(observation, field, hostile)
    with pytest.raises(ControllerInvariantError):
        _step_with_observation(observation)
    assert hostile.hooks == []


def test_step_result_retains_only_detached_canonical_observation_payload():
    payload = {"dispatch_reason": "ok", "nested": {"value": 1}}
    observation = Observation(
        "observation-000000000", "action-000000000", "run-1", "task-1",
        ActionName.RUN_TESTS.value, ObservationStatus.OK, payload, "summary", False,
    )
    step = _step_with_observation(observation)
    payload["nested"]["value"] = 2
    assert step.observation is not observation
    assert step.observation.payload == {"dispatch_reason": "ok", "nested": {"value": 1}}


@pytest.mark.parametrize("field", [
    "observation_id", "action_id", "run_id", "task_id", "name", "status",
    "payload", "summary", "truncated",
])
def test_malformed_registry_observation_with_hostile_scalar_is_bounded(field, monkeypatch):
    hostile = _HostileObservationScalar()
    values = {
        "observation_id": "observation-000000000",
        "action_id": "action-000000000",
        "run_id": "run-1",
        "task_id": "task-1",
        "name": ActionName.RUN_TESTS.value,
        "status": ObservationStatus.OK,
        "payload": {"dispatch_reason": "ok"},
        "summary": "summary",
        "truncated": False,
    }
    values[field] = hostile

    def dispatch(registry, action, *, observation_id):
        return Observation(**values)

    monkeypatch.setattr(ToolRegistry, "dispatch", dispatch)
    result = DeterministicController(
        registry_for(ActionName.RUN_TESTS)[0],
        scripted((ActionDirective(ActionName.RUN_TESTS, {}),)),
        ControllerRunConfig(1),
    ).run(snapshot())
    assert result.final_state is ControllerState.FAILED
    assert result.stop_reason is ControllerStopReason.CONTROLLER_ERROR
    assert result.steps[0].observation is None
    assert result.last_observation is None
    assert result.budget_state == ControllerBudgetState()
    assert hostile.hooks == []


def _run_fake_observation(monkeypatch, payload):
    def dispatch(registry, action, *, observation_id):
        return Observation(
            observation_id, action.action_id, action.run_id, action.task_id,
            action.name, ObservationStatus.OK, payload, "fake", False,
        )

    monkeypatch.setattr(ToolRegistry, "dispatch", dispatch)
    registry, _ = registry_for(ActionName.RUN_TESTS)
    return DeterministicController(registry, scripted((ActionDirective(ActionName.RUN_TESTS, {}),))).run(snapshot())


def test_hostile_observation_key_is_rejected_before_mapping_lookup(monkeypatch):
    key = _HostileKey()
    payload = {"dispatch_reason": "ok", key: "bad"}
    key.hooks.clear()
    key.raise_on_hash = True
    result = _run_fake_observation(monkeypatch, payload)
    assert result.stop_reason is ControllerStopReason.CONTROLLER_ERROR
    assert result.steps[0].observation is None
    assert result.budget_state == ControllerBudgetState()
    assert key.hooks == []


def test_string_subclass_key_is_rejected_without_hooks(monkeypatch):
    hooks = []
    key = _StringKey("bad", hooks)
    key.raise_on_hash = False
    payload = {"dispatch_reason": "ok", key: "bad"}
    hooks.clear()
    key.raise_on_hash = True
    result = _run_fake_observation(monkeypatch, payload)
    assert result.stop_reason is ControllerStopReason.CONTROLLER_ERROR
    assert result.steps[0].observation is None
    assert hooks == []


@pytest.mark.parametrize("payload", [
    {"dispatch_reason": "ok", "bad": (1, 2)},
    {"dispatch_reason": "ok", "bad": {1, 2}},
    {"dispatch_reason": "ok", "bad": b"bytes"},
    {"dispatch_reason": "ok", "bad": float("nan")},
    {"dispatch_reason": "ok", "bad": float("inf")},
    {"dispatch_reason": "ok", "bad": object()},
])
def test_invalid_json_observation_payload_is_bounded(monkeypatch, payload):
    result = _run_fake_observation(monkeypatch, payload)
    assert result.stop_reason is ControllerStopReason.CONTROLLER_ERROR
    assert result.steps[0].observation is None
    assert result.budget_state == ControllerBudgetState()


def test_cyclic_json_observation_payload_is_bounded(monkeypatch):
    cyclic = []
    cyclic.append(cyclic)
    result = _run_fake_observation(monkeypatch, {"dispatch_reason": "ok", "bad": cyclic})
    assert result.stop_reason is ControllerStopReason.CONTROLLER_ERROR
    assert result.steps[0].observation is None


def test_custom_mapping_and_dict_subclass_observations_are_rejected(monkeypatch):
    custom = _CustomMapping()
    result = _run_fake_observation(monkeypatch, custom)
    assert result.stop_reason is ControllerStopReason.CONTROLLER_ERROR
    assert custom.hooks == []

    class MappingSubclass(dict):
        def items(self):
            raise AssertionError("items")

    result = _run_fake_observation(monkeypatch, MappingSubclass(dispatch_reason="ok"))
    assert result.stop_reason is ControllerStopReason.CONTROLLER_ERROR


def test_malformed_observation_then_clean_run_same_controller(monkeypatch):
    registry, _ = registry_for(ActionName.RUN_TESTS)
    adapter = scripted((ActionDirective(ActionName.RUN_TESTS, {}),))
    controller = DeterministicController(registry, adapter, ControllerRunConfig(1))
    original_dispatch = ToolRegistry.dispatch
    monkeypatch.setattr(ToolRegistry, "dispatch", lambda *args, **kwargs: object())
    failed = controller.run(snapshot())
    assert failed.stop_reason is ControllerStopReason.CONTROLLER_ERROR
    monkeypatch.setattr(ToolRegistry, "dispatch", original_dispatch)
    clean = controller.run(snapshot())
    assert clean.stop_reason is ControllerStopReason.MODEL_CALL_LIMIT
    assert clean.steps[0].observation is not None


def test_failed_run_then_valid_run_same_controller_has_no_cursor():
    registry, _ = registry_for(ActionName.RUN_TESTS)
    adapter = _QueueAdapter([
        object(),
        ActionDirective(ActionName.RUN_TESTS, {}),
    ])
    controller = DeterministicController(registry, adapter, ControllerRunConfig(1))
    failed = controller.run(snapshot())
    valid = controller.run(snapshot())
    assert failed.stop_reason is ControllerStopReason.MODEL_ERROR
    assert valid.stop_reason is ControllerStopReason.MODEL_CALL_LIMIT
    assert valid.steps[0].action.action_id == "action-000000000"
    assert adapter.calls == 2
