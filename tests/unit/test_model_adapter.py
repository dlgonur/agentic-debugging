from __future__ import annotations

# no runtime dependencies
from dataclasses import FrozenInstanceError

import pytest

from agentic_debugger.agent.controller_policy import (
    ActionName,
    ControllerBudgetLimits,
    ControllerBudgetState,
    HypothesisConfidence,
    HypothesisLedger,
    HypothesisStatus,
    RootCauseHypothesis,
)
from agentic_debugger.agent.model_adapter import (
    MAX_MODEL_ARGUMENT_BYTES,
    MAX_MODEL_JSON_DEPTH,
    MAX_MODEL_JSON_NODES,
    MAX_MODEL_NAME_BYTES,
    MAX_MODEL_REASON_BYTES,
    ActionDirective,
    AddHypothesisDirective,
    ControllerSnapshot,
    ModelAdapter,
    ModelAdapterError,
    ModelDirectiveKind,
    ModelScriptExhaustedError,
    ModelScriptMismatchError,
    ReviseHypothesisDirective,
    ScriptedModelAdapter,
    ScriptedModelStep,
    SetHypothesisStatusDirective,
    TransitionDirective,
    directive_kind,
)
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.events.schema import Observation, ObservationStatus


LIMITS = ControllerBudgetLimits(2, 4, 2, max_active_hypotheses=3, max_source_observations=4)


def hypothesis(identifier: str, status=HypothesisStatus.ACTIVE):
    return RootCauseHypothesis(
        identifier,
        identifier + " statement",
        HypothesisConfidence.LOW,
        status,
        (identifier + "-evidence",),
        False,
        1,
    )


def observation(run_id="run-1", task_id="task-1"):
    return Observation(
        "obs-1", "action-1", run_id, task_id, "tool", ObservationStatus.OK,
        {"sensitive": "payload"}, "summary", False,
    )


def snapshot(index=0, state=ControllerState.REPRODUCE, ledger=None, last=None, budget=None):
    return ControllerSnapshot(
        "run-1", "task-1", state, index, LIMITS,
        ControllerBudgetState() if budget is None else budget,
        HypothesisLedger() if ledger is None else ledger,
        last,
    )


def directive_set():
    return (
        ActionDirective(ActionName.RUN_TESTS, {"argv": ["pytest"]}),
        TransitionDirective(ControllerState.UNDERSTAND, "tests reproduced"),
        AddHypothesisDirective("h-1", "the value is missing", HypothesisConfidence.LOW),
        ReviseHypothesisDirective("h-1", "the value is still missing", HypothesisConfidence.MEDIUM, (), True),
        SetHypothesisStatusDirective("h-1", HypothesisStatus.DISCARDED),
    )


@pytest.mark.parametrize(
    ("directive", "kind"),
    list(zip(directive_set(), ModelDirectiveKind)),
)
def test_directive_kind_is_exact(directive, kind):
    assert directive.kind is kind
    assert directive_kind(directive) is kind


def test_directive_kind_rejects_duck_types_and_subclasses_without_attribute_access():
    class Hostile:
        @property
        def kind(self):
            raise AssertionError("accessed")

    class ActionChild(ActionDirective):
        pass

    with pytest.raises(ModelAdapterError):
        directive_kind(Hostile())
    with pytest.raises(ModelAdapterError):
        directive_kind(ActionChild(ActionName.RUN_TESTS, {}))


def test_action_arguments_are_bounded_json_and_detached():
    source = {"nested": {"items": [1, True, None, 1.5, "text"]}}
    directive = ActionDirective(ActionName.RUN_TESTS, source)
    source["nested"]["items"].append("caller")
    source["nested"]["new"] = "caller"
    assert directive.arguments == {"nested": {"items": [1, True, None, 1.5, "text"]}}
    assert directive.arguments["nested"] is not source["nested"]
    with pytest.raises(FrozenInstanceError):
        directive.name = ActionName.RUN_REPRODUCTION


@pytest.mark.parametrize("bad", [(1,), {1}, frozenset({1}), b"x", bytearray(b"x"), object()])
def test_action_rejects_non_exact_json_values(bad):
    with pytest.raises(ModelAdapterError):
        ActionDirective(ActionName.RUN_TESTS, {"bad": bad})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_action_rejects_non_finite_float(bad):
    with pytest.raises(ModelAdapterError):
        ActionDirective(ActionName.RUN_TESTS, {"bad": bad})


def test_action_rejects_subclasses_keys_and_containers():
    class DictChild(dict):
        pass

    class ListChild(list):
        pass

    class StrChild(str):
        pass

    with pytest.raises(ModelAdapterError):
        ActionDirective(ActionName.RUN_TESTS, DictChild())
    with pytest.raises(ModelAdapterError):
        ActionDirective(ActionName.RUN_TESTS, {"bad": ListChild()})
    with pytest.raises(ModelAdapterError):
        ActionDirective(ActionName.RUN_TESTS, {StrChild("bad"): 1})


@pytest.mark.parametrize("name", ["run_tests", ActionName.RUN_TESTS.value])
def test_action_name_must_be_exact_enum(name):
    with pytest.raises(ModelAdapterError):
        ActionDirective(name, {})


def test_action_rejects_cycles_and_boundaries():
    cyclic_list = []
    cyclic_list.append(cyclic_list)
    cyclic_dict = {}
    cyclic_dict["self"] = cyclic_dict
    with pytest.raises(ModelAdapterError):
        ActionDirective(ActionName.RUN_TESTS, {"x": cyclic_list})
    with pytest.raises(ModelAdapterError):
        ActionDirective(ActionName.RUN_TESTS, cyclic_dict)

    nested = value = []
    for _ in range(MAX_MODEL_JSON_DEPTH - 1):
        child = []
        value.append(child)
        value = child
    ActionDirective(ActionName.RUN_TESTS, {"root": nested})
    too_deep = value = []
    for _ in range(MAX_MODEL_JSON_DEPTH + 1):
        child = []
        value.append(child)
        value = child
    with pytest.raises(ModelAdapterError):
        ActionDirective(ActionName.RUN_TESTS, {"root": too_deep})

    many = {"x": [None] * (MAX_MODEL_JSON_NODES - 2)}
    ActionDirective(ActionName.RUN_TESTS, many)
    with pytest.raises(ModelAdapterError):
        ActionDirective(ActionName.RUN_TESTS, {"x": [None] * (MAX_MODEL_JSON_NODES - 1)})


def test_action_byte_limit_is_utf8_and_compact():
    ActionDirective(ActionName.RUN_TESTS, {"x": "é" * 16380})
    with pytest.raises(ModelAdapterError):
        ActionDirective(ActionName.RUN_TESTS, {"x": "é" * 16381})
    with pytest.raises(ModelAdapterError):
        ActionDirective(ActionName.RUN_TESTS, {"x": "x" * MAX_MODEL_ARGUMENT_BYTES})


@pytest.mark.parametrize("bad", ["", " reason", "reason ", "bad\x00", "bad\x7f"])
def test_transition_reason_validation(bad):
    with pytest.raises(ModelAdapterError):
        TransitionDirective(ControllerState.DONE, bad)


def test_transition_does_not_validate_source_transition():
    directive = TransitionDirective(ControllerState.DONE, "terminal")
    assert directive.target_state is ControllerState.DONE
    with pytest.raises(ModelAdapterError):
        TransitionDirective("Done", "terminal")
    TransitionDirective(ControllerState.DONE, "é" * (MAX_MODEL_REASON_BYTES // 2))
    with pytest.raises(ModelAdapterError):
        TransitionDirective(ControllerState.DONE, "é" * (MAX_MODEL_REASON_BYTES // 2 + 1))


def test_hypothesis_directives_reuse_task_5a_validation_without_ledger_mutation():
    original = HypothesisLedger()
    add = AddHypothesisDirective("h-1", "candidate", HypothesisConfidence.HIGH, ("e-1", "e-2"), True)
    revise = ReviseHypothesisDirective("h-1", "revised", HypothesisConfidence.MEDIUM, ("e-2",), False)
    assert add.kind is ModelDirectiveKind.ADD_HYPOTHESIS
    assert revise.kind is ModelDirectiveKind.REVISE_HYPOTHESIS
    assert original.hypotheses == ()
    assert add.evidence_refs == ("e-1", "e-2")
    for bad in ["active", HypothesisStatus.ACTIVE]:
        with pytest.raises(ModelAdapterError):
            SetHypothesisStatusDirective("h-1", bad)
    for status in (HypothesisStatus.SUPPORTED, HypothesisStatus.REJECTED, HypothesisStatus.DISCARDED):
        assert SetHypothesisStatusDirective("h-1", status).status is status
    with pytest.raises(ModelAdapterError):
        AddHypothesisDirective("h-1", "candidate", HypothesisConfidence.LOW, ("e", "e"))
    with pytest.raises(ModelAdapterError):
        ReviseHypothesisDirective("h-1", "candidate", HypothesisConfidence.LOW, ["e"], False)


def test_hypothesis_errors_do_not_leak_content():
    statement = "SECRET-STATEMENT"
    evidence = "SECRET-EVIDENCE"
    with pytest.raises(ModelAdapterError) as caught:
        AddHypothesisDirective("h-1", statement, HypothesisConfidence.LOW, (evidence, evidence))
    assert statement not in str(caught.value)
    assert evidence not in str(caught.value)


def test_snapshot_validates_budget_observation_and_ordering():
    ledger = HypothesisLedger((hypothesis("first"), hypothesis("terminal", HypothesisStatus.DISCARDED)))
    snap = snapshot(ledger=ledger, last=observation(), budget=ControllerBudgetState(2, 4, 2, 4))
    assert snap.allowed_actions == tuple(a for a in ActionName if a in {
        ActionName.RUN_TESTS, ActionName.RUN_REPRODUCTION, ActionName.GET_FAILURE_TRACE,
    })
    assert snap.active_hypotheses == (ledger.hypotheses[0],)
    assert snap.hypotheses is ledger
    assert snap.budget_state == ControllerBudgetState(2, 4, 2, 4)
    for field, limit in (("patch_attempts", "max_patch_attempts"), ("test_runs", "max_test_runs"),
                         ("pdb_observations", "max_pdb_observations"), ("source_observations", "max_source_observations")):
        values = {"patch_attempts": 0, "test_runs": 0, "pdb_observations": 0, "source_observations": 0}
        values[field] = getattr(LIMITS, limit) + 1
        with pytest.raises(ModelAdapterError):
            snapshot(budget=ControllerBudgetState(**values))
    with pytest.raises(ModelAdapterError):
        snapshot(last=observation("other", "task-1"))
    with pytest.raises(ModelAdapterError):
        snapshot(last=observation("run-1", "other"))


@pytest.mark.parametrize("field", ["run_id", "task_id"])
def test_snapshot_identifier_validation(field):
    values = {"run_id": "run-1", "task_id": "task-1"}
    for bad in ["", " run-1", "run-1 ", "bad\x00", "é" * 129]:
        values[field] = bad
        with pytest.raises(ModelAdapterError):
            ControllerSnapshot(values["run_id"], values["task_id"], ControllerState.REPRODUCE, 0, LIMITS, ControllerBudgetState(), HypothesisLedger())


@pytest.mark.parametrize("state", list(ControllerState))
def test_snapshot_allowed_actions_are_canonical_and_terminal_empty(state):
    snap = snapshot(state=state)
    assert snap.allowed_actions == tuple(a for a in ActionName if a in set(snap.allowed_actions))
    if state in (ControllerState.DONE, ControllerState.FAILED):
        assert snap.allowed_actions == ()


def test_scripted_adapter_is_indexed_only_by_snapshot_and_is_deterministic():
    first = ActionDirective(ActionName.RUN_TESTS, {"nested": {"items": [1]}})
    second = TransitionDirective(ControllerState.UNDERSTAND, "done")
    adapter = ScriptedModelAdapter((
        ScriptedModelStep(ControllerState.REPRODUCE, first),
        ScriptedModelStep(ControllerState.UNDERSTAND, second),
    ))
    zero = snapshot(0)
    one = snapshot(1, ControllerState.UNDERSTAND)
    assert hasattr(adapter, "model_name") and hasattr(adapter, "next_directive")
    first_return = adapter.next_directive(zero)
    assert first_return == first
    assert first_return is not first
    second_return = adapter.next_directive(one)
    assert second_return == second
    zero_again = adapter.next_directive(zero)
    assert zero_again == first_return
    assert zero_again is not first_return
    assert zero_again.arguments is not first_return.arguments
    assert zero_again.arguments["nested"] is not first_return.arguments["nested"]
    assert zero_again.arguments["nested"]["items"] is not first_return.arguments["nested"]["items"]
    assert adapter.steps == adapter.steps
    assert adapter.model_name == "scripted"
    with pytest.raises(ModelScriptExhaustedError):
        adapter.next_directive(snapshot(2, ControllerState.UNDERSTAND))
    with pytest.raises(ModelScriptMismatchError) as caught:
        adapter.next_directive(snapshot(1, ControllerState.REPRODUCE))
    assert "done" not in str(caught.value)


def test_scripted_canonical_ownership_survives_all_caller_mutations():
    original_arguments = {"nested": {"items": ["stable"]}}
    directive = ActionDirective(ActionName.RUN_TESTS, original_arguments)
    step = ScriptedModelStep(ControllerState.REPRODUCE, directive)
    adapter = ScriptedModelAdapter((step,))
    original_arguments["nested"]["items"].append("caller")
    object.__setattr__(step, "expected_state", ControllerState.DONE)
    object.__setattr__(directive, "name", object())
    object.__setattr__(directive, "arguments", {"nested": {"items": ["forged"]}})
    result = adapter.next_directive(snapshot(0))
    assert result == ActionDirective(ActionName.RUN_TESTS, {"nested": {"items": ["stable"]}})
    assert type(result) is ActionDirective
    assert result.arguments["nested"]["items"] == ["stable"]


def test_scripted_returned_arguments_are_independently_mutable_and_detached():
    adapter = ScriptedModelAdapter((ScriptedModelStep(
        ControllerState.REPRODUCE,
        ActionDirective(ActionName.RUN_TESTS, {"nested": {"items": [1]}}),
    ),))
    first = adapter.next_directive(snapshot(0))
    first.arguments["new"] = "only-first"
    first.arguments["nested"]["added"] = "only-first"
    first.arguments["nested"]["items"].append(2)
    second = adapter.next_directive(snapshot(0))
    assert second == ActionDirective(ActionName.RUN_TESTS, {"nested": {"items": [1]}})
    assert "new" not in second.arguments
    assert "added" not in second.arguments["nested"]
    assert second.arguments["nested"]["items"] == [1]
    assert first.arguments is not second.arguments
    assert first.arguments["nested"] is not second.arguments["nested"]
    assert first.arguments["nested"]["items"] is not second.arguments["nested"]["items"]
    assert not hasattr(adapter, "_cursor")


def test_scripted_adapter_contract_and_model_name():
    step = ScriptedModelStep(ControllerState.REPRODUCE, ActionDirective(ActionName.RUN_TESTS, {}))
    ScriptedModelAdapter((step,), "vendor/model-1.2")
    with pytest.raises(ModelAdapterError):
        ScriptedModelAdapter([], "scripted")
    with pytest.raises(ModelAdapterError):
        ScriptedModelAdapter((), "scripted")
    with pytest.raises(ModelAdapterError):
        ScriptedModelAdapter((step,), "model name")
    with pytest.raises(ModelAdapterError):
        ScriptedModelAdapter((step,), "é" * (MAX_MODEL_NAME_BYTES // 2 + 1))
    with pytest.raises(ModelAdapterError):
        ScriptedModelAdapter((step,), "scripted!")
    with pytest.raises(ModelAdapterError):
        ScriptedModelAdapter((step,), [step])


def test_scripted_step_requires_exact_directive_and_state():
    step = ScriptedModelStep(ControllerState.REPRODUCE, ActionDirective(ActionName.RUN_TESTS, {}))
    assert step.expected_state is ControllerState.REPRODUCE
    with pytest.raises(ModelAdapterError):
        ScriptedModelStep("Reproduce", step.directive)
    class StepChild(ScriptedModelStep):
        pass
    with pytest.raises(ModelAdapterError):
        ScriptedModelAdapter((StepChild(step.expected_state, step.directive),))


class _HostileIndex:
    def __init__(self):
        self.hooks = []
    def _trip(self, name):
        self.hooks.append(name)
        raise AssertionError(name)
    def __ge__(self, other): return self._trip("ge")
    def __gt__(self, other): return self._trip("gt")
    def __lt__(self, other): return self._trip("lt")
    def __le__(self, other): return self._trip("le")
    def __index__(self): return self._trip("index")
    def __int__(self): return self._trip("int")
    def __repr__(self): return self._trip("repr")
    def __str__(self): return self._trip("str")


@pytest.mark.parametrize("bad_index", [-1, True, False, 1.0, "0"])
def test_next_directive_revalidates_forged_index_before_script_access(bad_index):
    adapter = ScriptedModelAdapter((ScriptedModelStep(
        ControllerState.REPRODUCE, ActionDirective(ActionName.RUN_TESTS, {}),
    ),))
    forged = snapshot(0)
    object.__setattr__(forged, "model_call_index", bad_index)
    with pytest.raises(ModelAdapterError):
        adapter.next_directive(forged)


def test_next_directive_rejects_hostile_forged_index_without_hooks():
    adapter = ScriptedModelAdapter((ScriptedModelStep(
        ControllerState.REPRODUCE, ActionDirective(ActionName.RUN_TESTS, {}),
    ),))
    forged = snapshot(0)
    hostile = _HostileIndex()
    object.__setattr__(forged, "model_call_index", hostile)
    with pytest.raises(ModelAdapterError):
        adapter.next_directive(forged)
    assert hostile.hooks == []


@pytest.mark.parametrize("bad_state", ["Reproduce", object()])
def test_next_directive_revalidates_forged_state(bad_state):
    adapter = ScriptedModelAdapter((ScriptedModelStep(
        ControllerState.REPRODUCE, ActionDirective(ActionName.RUN_TESTS, {}),
    ),))
    forged = snapshot(0)
    object.__setattr__(forged, "state", bad_state)
    with pytest.raises(ModelAdapterError):
        adapter.next_directive(forged)


@pytest.mark.parametrize("field,value", [
    ("budget_limits", object()),
    ("budget_state", object()),
    ("hypotheses", object()),
    ("last_observation", object()),
])
def test_next_directive_revalidates_forged_snapshot_records(field, value):
    adapter = ScriptedModelAdapter((ScriptedModelStep(
        ControllerState.REPRODUCE, ActionDirective(ActionName.RUN_TESTS, {}),
    ),))
    forged = snapshot(0)
    object.__setattr__(forged, field, value)
    with pytest.raises(ModelAdapterError):
        adapter.next_directive(forged)


def test_next_directive_rejects_forged_over_limit_budget_without_script_access():
    adapter = ScriptedModelAdapter((ScriptedModelStep(
        ControllerState.REPRODUCE, ActionDirective(ActionName.RUN_TESTS, {}),
    ),))
    forged = snapshot(0)
    object.__setattr__(forged, "budget_state", ControllerBudgetState(patch_attempts=LIMITS.max_patch_attempts + 1))
    with pytest.raises(ModelAdapterError):
        adapter.next_directive(forged)


def test_next_directive_rejects_forged_canonical_step_directive():
    adapter = ScriptedModelAdapter((ScriptedModelStep(
        ControllerState.REPRODUCE, ActionDirective(ActionName.RUN_TESTS, {}),
    ),))
    object.__setattr__(adapter, "_canonical_steps", (object(),))
    with pytest.raises(ModelAdapterError):
        adapter.next_directive(snapshot(0))



def active_ledger(count, terminal_count=0):
    records = [hypothesis(f"active-{index}") for index in range(count)]
    records.extend(
        hypothesis(f"terminal-{index}", HypothesisStatus.DISCARDED)
        for index in range(terminal_count)
    )
    return HypothesisLedger(tuple(records))


def test_snapshot_active_capacity_zero_and_exact_limit():
    assert snapshot(ledger=active_ledger(0)).active_hypotheses == ()
    at_limit = snapshot(ledger=active_ledger(LIMITS.max_active_hypotheses))
    assert len(at_limit.active_hypotheses) == LIMITS.max_active_hypotheses


def test_snapshot_rejects_over_capacity_active_ledger():
    secret_statement = "SECRET-HYPOTHESIS-STATEMENT"
    secret_evidence = "SECRET-HYPOTHESIS-EVIDENCE"
    records = [hypothesis(f"active-{index}") for index in range(3)]
    records.append(RootCauseHypothesis(
        "overflow", secret_statement, HypothesisConfidence.HIGH,
        HypothesisStatus.ACTIVE, (secret_evidence,), False, 1,
    ))
    with pytest.raises(ModelAdapterError) as caught:
        snapshot(ledger=HypothesisLedger(tuple(records)))
    assert secret_statement not in str(caught.value)
    assert secret_evidence not in str(caught.value)


@pytest.mark.parametrize("status", [
    HypothesisStatus.SUPPORTED,
    HypothesisStatus.REJECTED,
    HypothesisStatus.DISCARDED,
])
def test_terminal_records_do_not_consume_snapshot_active_capacity(status):
    terminal = RootCauseHypothesis("terminal", "terminal statement", HypothesisConfidence.LOW, status, (), False, 1)
    ledger = HypothesisLedger(tuple(active_ledger(3).hypotheses) + (terminal,))
    snap = snapshot(ledger=ledger)
    assert len(snap.active_hypotheses) == 3
    assert snap.hypotheses.hypotheses[-1].status is status


def test_snapshot_rejects_active_capacity_with_terminal_records_too():
    with pytest.raises(ModelAdapterError):
        snapshot(ledger=active_ledger(4, terminal_count=10))


def test_larger_capacity_ledger_fails_under_smaller_snapshot_limit():
    larger_limit_ledger = active_ledger(3)
    smaller_limits = ControllerBudgetLimits(2, 4, 2, max_active_hypotheses=2, max_source_observations=4)
    with pytest.raises(ModelAdapterError):
        ControllerSnapshot(
            "run-1", "task-1", ControllerState.REPRODUCE, 0,
            smaller_limits, ControllerBudgetState(), larger_limit_ledger,
        )


def test_forged_over_capacity_ledger_is_rejected_by_snapshot_properties():
    valid = snapshot(ledger=active_ledger(3))
    object.__setattr__(valid, "hypotheses", active_ledger(4))
    with pytest.raises(ModelAdapterError):
        valid.allowed_actions
    with pytest.raises(ModelAdapterError):
        valid.active_hypotheses


def test_adapter_rejects_over_capacity_before_script_access_and_recovers():
    adapter = ScriptedModelAdapter((ScriptedModelStep(
        ControllerState.REPRODUCE, ActionDirective(ActionName.RUN_TESTS, {"stable": True}),
    ),))
    over_capacity = snapshot(ledger=active_ledger(3))
    object.__setattr__(over_capacity, "hypotheses", active_ledger(4))
    with pytest.raises(ModelAdapterError):
        adapter.next_directive(over_capacity)
    valid_result = adapter.next_directive(snapshot())
    assert valid_result == ActionDirective(ActionName.RUN_TESTS, {"stable": True})
    assert adapter.steps[0].directive.arguments == {"stable": True}


class _HostileStatus:
    def __init__(self):
        self.hooks = []
    def _trip(self, name):
        self.hooks.append(name)
        raise AssertionError(name)
    def __eq__(self, other): return self._trip("eq")
    def __hash__(self): return self._trip("hash")
    def __repr__(self): return self._trip("repr")
    def __str__(self): return self._trip("str")


def test_forged_status_is_rejected_without_hooks():
    record = hypothesis("hostile-status")
    ledger = HypothesisLedger((record,))
    hostile = _HostileStatus()
    object.__setattr__(record, "status", hostile)
    with pytest.raises(ModelAdapterError):
        snapshot(ledger=ledger)
    assert hostile.hooks == []


def test_forged_duplicate_ids_are_rejected_after_exact_validation():
    first = hypothesis("first")
    second = hypothesis("second")
    ledger = HypothesisLedger((first, second))
    object.__setattr__(second, "hypothesis_id", "first")
    with pytest.raises(ModelAdapterError):
        snapshot(ledger=ledger)



class _ShadowCallable:
    def __init__(self):
        self.hooks = []
    def __call__(self, *args, **kwargs):
        self.hooks.append("called")
        raise RuntimeError("shadow called")


def shadow_methods(record, names):
    shadows = {}
    for name in names:
        shadow = _ShadowCallable()
        object.__setattr__(record, name, shadow)
        shadows[name] = shadow
    return shadows


def test_budget_method_shadows_are_ignored_for_valid_snapshot_and_adapter():
    state = ControllerBudgetState()
    shadows = shadow_methods(state, ["remaining", "consumed", "can_consume", "consume"])
    snap = snapshot(budget=state)
    assert snap.budget_state is state
    adapter = ScriptedModelAdapter((ScriptedModelStep(
        ControllerState.REPRODUCE, ActionDirective(ActionName.RUN_TESTS, {}),
    ),))
    assert type(adapter.next_directive(snap)) is ActionDirective
    assert all(shadow.hooks == [] for shadow in shadows.values())


@pytest.mark.parametrize(
    ("field", "limit_field"),
    [
        ("patch_attempts", "max_patch_attempts"),
        ("test_runs", "max_test_runs"),
        ("pdb_observations", "max_pdb_observations"),
        ("source_observations", "max_source_observations"),
    ],
)
def test_budget_method_shadow_cannot_bypass_over_limit(field, limit_field):
    values = {"patch_attempts": 0, "test_runs": 0, "pdb_observations": 0, "source_observations": 0}
    values[field] = getattr(LIMITS, limit_field) + 1
    state = ControllerBudgetState(**values)
    shadows = shadow_methods(state, ["remaining", "consumed", "can_consume", "consume"])
    with pytest.raises(ModelAdapterError):
        snapshot(budget=state)
    assert all(shadow.hooks == [] for shadow in shadows.values())


@pytest.mark.parametrize("name", ["remaining", "consumed"])
def test_non_callable_budget_method_shadows_are_ignored(name):
    state = ControllerBudgetState()
    object.__setattr__(state, name, object())
    assert snapshot(budget=state).budget_state is state


def test_ledger_method_shadows_are_ignored_and_active_records_are_reconstructed():
    ledger = active_ledger(2, terminal_count=2)
    shadows = shadow_methods(ledger, ["active_hypotheses", "get", "add", "revise", "transition"])
    snap = snapshot(ledger=ledger)
    assert snap.allowed_actions
    returned = snap.active_hypotheses
    assert tuple(record.hypothesis_id for record in returned) == ("active-0", "active-1")
    assert returned[0] == ledger.hypotheses[0]
    assert returned[0] is not ledger.hypotheses[0]
    adapter = ScriptedModelAdapter((ScriptedModelStep(
        ControllerState.REPRODUCE, ActionDirective(ActionName.RUN_TESTS, {}),
    ),))
    assert type(adapter.next_directive(snap)) is ActionDirective
    assert all(shadow.hooks == [] for shadow in shadows.values())


def test_forged_ledger_active_return_is_ignored_and_cannot_bypass_capacity():
    ledger = active_ledger(3)
    forged_extra = hypothesis("forged-extra")
    object.__setattr__(ledger, "active_hypotheses", lambda: (forged_extra,))
    snap = snapshot(ledger=ledger)
    assert tuple(record.hypothesis_id for record in snap.active_hypotheses) == (
        "active-0", "active-1", "active-2",
    )
    over_capacity = active_ledger(4)
    object.__setattr__(over_capacity, "active_hypotheses", lambda: ())
    forged_snapshot = snapshot(ledger=ledger)
    object.__setattr__(forged_snapshot, "hypotheses", over_capacity)
    with pytest.raises(ModelAdapterError):
        forged_snapshot.active_hypotheses


def test_method_shadows_do_not_change_failure_then_valid_adapter_call():
    state = ControllerBudgetState(test_runs=LIMITS.max_test_runs + 1)
    budget_shadow = _ShadowCallable()
    object.__setattr__(state, "remaining", budget_shadow)
    ledger = active_ledger(3)
    ledger_shadow = _ShadowCallable()
    object.__setattr__(ledger, "active_hypotheses", ledger_shadow)
    valid_snapshot = snapshot()
    forged_snapshot = valid_snapshot
    object.__setattr__(forged_snapshot, "budget_state", state)
    object.__setattr__(forged_snapshot, "hypotheses", ledger)
    adapter = ScriptedModelAdapter((ScriptedModelStep(
        ControllerState.REPRODUCE, ActionDirective(ActionName.RUN_TESTS, {}),
    ),))
    with pytest.raises(ModelAdapterError):
        adapter.next_directive(forged_snapshot)
    assert budget_shadow.hooks == []
    assert ledger_shadow.hooks == []
    assert type(adapter.next_directive(snapshot())) is ActionDirective
