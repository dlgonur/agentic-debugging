import pytest

from agentic_debugger.agent.controller_policy import (
    ControllerBudgetLimits,
    ControllerPolicyError,
    HypothesisConfidence,
    HypothesisLedger,
    HypothesisStatus,
)


LIMITS = ControllerBudgetLimits(1, 1, 1, max_active_hypotheses=2)


def add(ledger, hypothesis_id, statement=None, confidence=HypothesisConfidence.LOW):
    return ledger.add(
        LIMITS,
        hypothesis_id=hypothesis_id,
        statement=statement or hypothesis_id + " statement",
        confidence=confidence,
        evidence_refs=(hypothesis_id + "-evidence",),
    )


def test_add_get_order_and_active_capacity():
    ledger = add(add(HypothesisLedger(), "h-1"), "h-2")
    assert [h.hypothesis_id for h in ledger.active_hypotheses()] == ["h-1", "h-2"]
    assert ledger.get("h-1").revision == 1
    with pytest.raises(ControllerPolicyError):
        add(ledger, "h-3")
    with pytest.raises(ControllerPolicyError):
        add(ledger, "h-1")
    with pytest.raises(ControllerPolicyError):
        ledger.get("missing")


@pytest.mark.parametrize("status", [HypothesisStatus.SUPPORTED, HypothesisStatus.REJECTED, HypothesisStatus.DISCARDED])
def test_active_to_terminal_transition_preserves_audit_record(status):
    original = add(HypothesisLedger(), "h-1")
    transitioned = original.transition("h-1", status)
    record = transitioned.get("h-1")
    assert record.status is status
    assert record.revision == 1
    assert transitioned.hypotheses == (record,)
    assert transitioned.active_hypotheses() == ()
    assert original.get("h-1").status is HypothesisStatus.ACTIVE
    with pytest.raises(ControllerPolicyError):
        transitioned.transition("h-1", HypothesisStatus.REJECTED)
    with pytest.raises(ControllerPolicyError):
        transitioned.revise("h-1", statement="new", confidence=HypothesisConfidence.HIGH, evidence_refs=(), requires_runtime_evidence=True)
    with pytest.raises(ControllerPolicyError):
        transitioned.add(LIMITS, hypothesis_id="h-1", statement="reuse", confidence=HypothesisConfidence.LOW)


def test_revision_replaces_evidence_and_preserves_record_order():
    original = add(add(HypothesisLedger(), "h-1"), "h-2")
    revised = original.revise(
        "h-1",
        statement="revised statement",
        confidence=HypothesisConfidence.MEDIUM,
        evidence_refs=("new-1", "new-2"),
        requires_runtime_evidence=True,
    )
    assert [h.hypothesis_id for h in revised.hypotheses] == ["h-1", "h-2"]
    assert revised.get("h-1").revision == 2
    assert revised.get("h-1").evidence_refs == ("new-1", "new-2")
    assert revised.get("h-1").statement == "revised statement"
    assert revised.get("h-1").requires_runtime_evidence is True
    assert original.get("h-1").revision == 1


def test_terminal_records_free_active_capacity_but_id_remains_reserved():
    ledger = add(add(HypothesisLedger(), "h-1"), "h-2")
    ledger = ledger.transition("h-1", HypothesisStatus.DISCARDED)
    ledger = add(ledger, "h-3")
    assert [h.hypothesis_id for h in ledger.hypotheses] == ["h-1", "h-2", "h-3"]
    assert [h.hypothesis_id for h in ledger.active_hypotheses()] == ["h-2", "h-3"]


@pytest.mark.parametrize("status", list(HypothesisStatus))
def test_illegal_active_and_same_status_transitions(status):
    ledger = add(HypothesisLedger(), "h-1")
    if status is HypothesisStatus.ACTIVE:
        with pytest.raises(ControllerPolicyError):
            ledger.transition("h-1", status)
    else:
        transitioned = ledger.transition("h-1", status)
        with pytest.raises(ControllerPolicyError):
            transitioned.transition("h-1", status)


class _HostileId:
    def __init__(self, marker):
        self.marker = marker

    def _trip(self, name):
        self.marker.append(name)
        raise RuntimeError(name)

    def __eq__(self, other):
        return self._trip("eq")

    def __hash__(self):
        return self._trip("hash")

    def __str__(self):
        return self._trip("str")

    def __repr__(self):
        return self._trip("repr")


class _HostileStr(str):
    def __new__(cls, value, marker):
        obj = str.__new__(cls, value)
        obj.marker = marker
        return obj

    def _trip(self, name):
        self.marker.append(name)
        raise RuntimeError(name)

    def __eq__(self, other):
        return self._trip("eq")

    def __hash__(self):
        return self._trip("hash")

    def __str__(self):
        return self._trip("str")

    def __repr__(self):
        return self._trip("repr")


@pytest.mark.parametrize("ledger_factory", [
    lambda: add(HypothesisLedger(), "existing"),
    lambda: add(add(HypothesisLedger(), "first"), "second"),
    lambda: HypothesisLedger().transition("terminal", HypothesisStatus.DISCARDED)
    if False else add(HypothesisLedger(), "terminal").transition("terminal", HypothesisStatus.DISCARDED),
])
def test_add_rejects_hostile_ids_before_scans_or_capacity(ledger_factory):
    ledger = ledger_factory()
    marker = []
    original = ledger
    with pytest.raises(ControllerPolicyError):
        ledger.add(
            LIMITS,
            hypothesis_id=_HostileId(marker),
            statement="candidate statement",
            confidence=HypothesisConfidence.LOW,
        )
    assert marker == []
    assert ledger is original
    assert ledger.hypotheses == original.hypotheses


def test_add_rejects_hostile_string_subclass_before_comparison():
    marker = []
    ledger = add(HypothesisLedger(), "same")
    with pytest.raises(ControllerPolicyError):
        ledger.add(
            LIMITS,
            hypothesis_id=_HostileStr("same", marker),
            statement="candidate statement",
            confidence=HypothesisConfidence.LOW,
        )
    assert marker == []
    assert [h.hypothesis_id for h in ledger.hypotheses] == ["same"]


def test_valid_duplicate_active_and_terminal_ids_remain_reserved():
    active = add(HypothesisLedger(), "same")
    with pytest.raises(ControllerPolicyError):
        active.add(LIMITS, hypothesis_id="same", statement="new", confidence=HypothesisConfidence.LOW)
    terminal = active.transition("same", HypothesisStatus.DISCARDED)
    with pytest.raises(ControllerPolicyError):
        terminal.add(LIMITS, hypothesis_id="same", statement="new", confidence=HypothesisConfidence.LOW)
    assert [h.hypothesis_id for h in terminal.hypotheses] == ["same"]


def test_malformed_candidate_is_validated_before_full_capacity_decision():
    limits = ControllerBudgetLimits(1, 1, 1, max_active_hypotheses=1)
    ledger = add(HypothesisLedger(), "existing")
    marker = []
    with pytest.raises(ControllerPolicyError) as caught:
        ledger.add(
            limits,
            hypothesis_id=_HostileStr("existing", marker),
            statement="candidate statement",
            confidence=HypothesisConfidence.LOW,
        )
    assert marker == []
    assert "candidate statement" not in str(caught.value)
    assert ledger.hypotheses == (ledger.get("existing"),)
