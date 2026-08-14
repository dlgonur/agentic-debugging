"""Session specification, identity, lifecycle, and result contract tests."""

from __future__ import annotations

import pytest

from agentic_debugger.application import ApplicationInputError
from agentic_debugger.application.events import (
    SessionPhase,
    SessionStatus,
    SessionTerminationReason,
    SourceKind,
    can_transition,
    compatible_reasons,
    terminal_status_for,
)
from agentic_debugger.application.session import (
    SessionBudgets,
    SessionId,
    SessionResult,
    SessionSnapshot,
    SessionSpec,
)
from application_support import VALID_RUN_ID, make_spec


class TestSessionId:
    @pytest.mark.parametrize(
        "value",
        ["session-1", "a", "1", "session_1", "session.1", "s" * 128],
    )
    def test_valid_identifiers(self, value):
        assert SessionId(value).value == value

    @pytest.mark.parametrize(
        "value",
        ["", "Bad ID", "Session-1", "s" * 129, " session", "session\x01", None, 42, "a=b"],
    )
    def test_invalid_identifiers(self, value):
        with pytest.raises(ApplicationInputError):
            SessionId(value)

    def test_identifier_is_immutable(self):
        session_id = SessionId("session-1")
        with pytest.raises(Exception):
            session_id.value = "other"

    def test_str_round_trip(self):
        assert str(SessionId("session-1")) == "session-1"


class TestSessionBudgets:
    def test_defaults_are_empty(self):
        budgets = SessionBudgets()
        assert budgets.max_model_calls is None
        assert budgets.max_controller_steps is None
        assert budgets.max_elapsed_seconds is None

    def test_valid_positive_budgets(self):
        budgets = SessionBudgets(max_model_calls=64, max_elapsed_seconds=900)
        assert budgets.max_model_calls == 64
        assert budgets.max_elapsed_seconds == 900

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"max_model_calls": 0},
            {"max_controller_steps": -1},
            {"max_elapsed_seconds": 0},
            {"max_model_calls": True},
            {"max_model_calls": "64"},
        ],
    )
    def test_invalid_budgets_rejected(self, kwargs):
        with pytest.raises(ApplicationInputError):
            SessionBudgets(**kwargs)


class TestSessionSpec:
    def test_valid_spec(self):
        spec = make_spec()
        assert spec.task_id == "curated-off-by-one-002"
        assert spec.source.kind is SourceKind.OFFLINE_DEMO
        assert spec.source.policy == "static-baseline"
        assert spec.artifact_destination is None

    def test_source_task_id_must_match_spec(self):
        from agentic_debugger.application.sources import ExecutionSourceSpec

        with pytest.raises(ApplicationInputError):
            SessionSpec(
                task_id="curated-off-by-one-002",
                source=ExecutionSourceSpec(
                    kind=SourceKind.OFFLINE_DEMO, task_id="curated-none-handling-001"
                ),
            )

    def test_invalid_task_id_rejected(self):
        from agentic_debugger.application.sources import ExecutionSourceSpec

        with pytest.raises(ApplicationInputError):
            SessionSpec(
                task_id="",
                source=ExecutionSourceSpec(
                    kind=SourceKind.OFFLINE_DEMO, task_id=""
                ),
            )

    def test_wrong_source_type_rejected(self):
        with pytest.raises(ApplicationInputError):
            SessionSpec(task_id="curated-off-by-one-002", source="offline")  # type: ignore[arg-type]

    def test_wrong_budgets_type_rejected(self):
        with pytest.raises(ApplicationInputError):
            SessionSpec(
                task_id="curated-off-by-one-002",
                source=make_spec().source,
                budgets={"max_model_calls": 1},  # type: ignore[arg-type]
            )

    def test_artifact_destination_bounds(self):
        with pytest.raises(ApplicationInputError):
            make_spec(artifact_destination="x" * 513)

    def test_fingerprint_is_stable_and_sensitive(self):
        spec_a = make_spec()
        spec_b = make_spec()
        spec_c = make_spec(policy="pdb-on-uncertainty")
        assert spec_a.fingerprint() == spec_b.fingerprint()
        assert spec_a.fingerprint() != spec_c.fingerprint()
        assert len(spec_a.fingerprint()) == 64


class TestLifecycleTaxonomy:
    def test_terminal_statuses(self):
        terminal = {
            status
            for status in SessionStatus
            if status.terminal
        }
        assert terminal == {
            SessionStatus.SUCCEEDED,
            SessionStatus.UNRESOLVED,
            SessionStatus.FAILED,
            SessionStatus.CANCELLED,
            SessionStatus.TIMED_OUT,
            SessionStatus.INTERRUPTED,
            SessionStatus.CLEANUP_FAILED,
        }

    def test_terminal_status_for(self):
        assert terminal_status_for(SessionTerminationReason.DONE) is SessionStatus.SUCCEEDED
        assert terminal_status_for(SessionTerminationReason.UNRESOLVED) is SessionStatus.UNRESOLVED
        assert terminal_status_for(SessionTerminationReason.TIMEOUT) is SessionStatus.TIMED_OUT
        assert terminal_status_for(SessionTerminationReason.CANCELLED) is SessionStatus.CANCELLED
        assert terminal_status_for(SessionTerminationReason.INTERRUPTED) is SessionStatus.INTERRUPTED
        assert terminal_status_for(SessionTerminationReason.CLEANUP_FAILED) is SessionStatus.CLEANUP_FAILED
        assert terminal_status_for(SessionTerminationReason.MODEL_ERROR) is SessionStatus.FAILED
        assert terminal_status_for(SessionTerminationReason.JOURNAL_ERROR) is SessionStatus.FAILED

    def test_compatible_reasons(self):
        assert compatible_reasons(SessionStatus.SUCCEEDED) == frozenset({SessionTerminationReason.DONE})
        assert SessionTerminationReason.VERIFIER_ERROR in compatible_reasons(SessionStatus.FAILED)
        assert SessionTerminationReason.CLEANUP_FAILED not in compatible_reasons(SessionStatus.FAILED)
        assert compatible_reasons(SessionStatus.RUNNING) == frozenset()

    @pytest.mark.parametrize(
        ("current", "target", "expected"),
        [
            (SessionStatus.CREATED, SessionStatus.STARTING, True),
            (SessionStatus.CREATED, SessionStatus.RUNNING, False),
            (SessionStatus.STARTING, SessionStatus.RUNNING, True),
            (SessionStatus.RUNNING, SessionStatus.RUNNING, True),
            (SessionStatus.RUNNING, SessionStatus.SUCCEEDED, True),
            (SessionStatus.RUNNING, SessionStatus.UNRESOLVED, True),
            (SessionStatus.RUNNING, SessionStatus.CANCELLED, True),
            (SessionStatus.RUNNING, SessionStatus.TIMED_OUT, True),
            (SessionStatus.RUNNING, SessionStatus.INTERRUPTED, True),
            (SessionStatus.RUNNING, SessionStatus.CLEANUP_FAILED, True),
            (SessionStatus.SUCCEEDED, SessionStatus.FAILED, False),
            (SessionStatus.CREATED, SessionStatus.CLEANUP_FAILED, False),
            (SessionStatus.SUCCEEDED, SessionStatus.CLEANUP_FAILED, False),
        ],
    )
    def test_can_transition(self, current, target, expected):
        assert can_transition(current, target) is expected

    def test_can_transition_rejects_non_status(self):
        with pytest.raises(Exception):
            can_transition("created", SessionStatus.RUNNING)  # type: ignore[arg-type]


class TestSessionSnapshot:
    def test_valid_running_snapshot(self):
        snapshot = SessionSnapshot(
            session_id=SessionId("session-1"),
            spec=make_spec(),
            status=SessionStatus.RUNNING,
            phase=SessionPhase.WAITING_MODEL,
            run_id=VALID_RUN_ID,
            sequence=4,
        )
        assert snapshot.status is SessionStatus.RUNNING
        assert snapshot.phase is SessionPhase.WAITING_MODEL
        assert snapshot.termination_reason is None

    def test_phase_requires_running(self):
        with pytest.raises(ApplicationInputError):
            SessionSnapshot(
                session_id=SessionId("session-1"),
                spec=make_spec(),
                status=SessionStatus.CREATED,
                phase=SessionPhase.WAITING_MODEL,
            )

    def test_terminal_requires_reason(self):
        with pytest.raises(ApplicationInputError):
            SessionSnapshot(
                session_id=SessionId("session-1"),
                spec=make_spec(),
                status=SessionStatus.SUCCEEDED,
            )

    def test_reason_must_be_compatible(self):
        with pytest.raises(ApplicationInputError):
            SessionSnapshot(
                session_id=SessionId("session-1"),
                spec=make_spec(),
                status=SessionStatus.SUCCEEDED,
                termination_reason=SessionTerminationReason.MODEL_ERROR,
            )

    def test_reason_invalid_while_active(self):
        with pytest.raises(ApplicationInputError):
            SessionSnapshot(
                session_id=SessionId("session-1"),
                spec=make_spec(),
                status=SessionStatus.RUNNING,
                termination_reason=SessionTerminationReason.DONE,
            )

    def test_sequence_must_be_non_negative(self):
        with pytest.raises(ApplicationInputError):
            SessionSnapshot(
                session_id=SessionId("session-1"),
                spec=make_spec(),
                status=SessionStatus.CREATED,
                sequence=-1,
            )

    def test_invalid_identifiers_rejected(self):
        with pytest.raises(ApplicationInputError):
            SessionSnapshot(
                session_id=SessionId("session-1"),
                spec=make_spec(),
                status=SessionStatus.RUNNING,
                run_id="bad\x00run",
            )


class TestSessionResult:
    def _valid_result(self, **overrides):
        values = {
            "session_id": SessionId("session-1"),
            "spec": make_spec(),
            "status": SessionStatus.SUCCEEDED,
            "termination_reason": SessionTerminationReason.DONE,
            "run_id": VALID_RUN_ID,
            "sequence": 9,
            "cleanup_verified": True,
        }
        values.update(overrides)
        return SessionResult(**values)

    def test_valid_result(self):
        result = self._valid_result()
        assert result.status is SessionStatus.SUCCEEDED
        assert result.cleanup_verified is True
        mapping = result.to_mapping()
        assert mapping["session_id"] == "session-1"
        assert mapping["status"] == "succeeded"
        assert mapping["termination_reason"] == "done"
        assert mapping["diagnostics"] == []

    def test_status_must_be_terminal(self):
        with pytest.raises(ApplicationInputError):
            self._valid_result(status=SessionStatus.RUNNING)

    def test_reason_must_be_compatible(self):
        with pytest.raises(ApplicationInputError):
            self._valid_result(
                status=SessionStatus.CANCELLED,
                termination_reason=SessionTerminationReason.DONE,
            )

    def test_cleanup_verified_must_be_boolean(self):
        with pytest.raises(ApplicationInputError):
            self._valid_result(cleanup_verified="yes")  # type: ignore[arg-type]

    def test_diagnostics_bounds(self):
        with pytest.raises(ApplicationInputError):
            self._valid_result(diagnostics=tuple(f"d{i}" for i in range(65)))
        with pytest.raises(ApplicationInputError):
            self._valid_result(diagnostics=("ok\x01",))

    def test_result_is_immutable(self):
        result = self._valid_result()
        with pytest.raises(Exception):
            result.status = SessionStatus.FAILED  # type: ignore[misc]


class TestSessionResultCleanupSemantics:
    """Blocker-2 coverage: SessionResult aligns with the stream contract.

    ``run_id`` is the accepted started indicator (bound by ``session.started``
    in the stream contract); cleanup rules use it instead of a hidden flag.
    """

    def _result(self, **overrides):
        values = {
            "session_id": SessionId("session-1"),
            "spec": make_spec(),
            "status": SessionStatus.SUCCEEDED,
            "termination_reason": SessionTerminationReason.DONE,
            "run_id": VALID_RUN_ID,
            "sequence": 9,
            "cleanup_verified": True,
        }
        values.update(overrides)
        return SessionResult(**values)

    @pytest.mark.parametrize("status", [SessionStatus.SUCCEEDED, SessionStatus.UNRESOLVED])
    def test_orderly_completion_requires_verified_cleanup(self, status):
        reason = (
            SessionTerminationReason.DONE
            if status is SessionStatus.SUCCEEDED
            else SessionTerminationReason.UNRESOLVED
        )
        with pytest.raises(ApplicationInputError):
            self._result(status=status, termination_reason=reason, cleanup_verified=False)
        result = self._result(status=status, termination_reason=reason)
        assert result.cleanup_verified is True

    @pytest.mark.parametrize("status", [SessionStatus.SUCCEEDED, SessionStatus.UNRESOLVED])
    def test_orderly_completion_requires_started_session(self, status):
        reason = (
            SessionTerminationReason.DONE
            if status is SessionStatus.SUCCEEDED
            else SessionTerminationReason.UNRESOLVED
        )
        with pytest.raises(ApplicationInputError):
            self._result(status=status, termination_reason=reason, run_id=None)

    def test_cancelled_started_session_requires_verified_cleanup(self):
        with pytest.raises(ApplicationInputError):
            self._result(
                status=SessionStatus.CANCELLED,
                termination_reason=SessionTerminationReason.CANCELLED,
                cleanup_verified=False,
            )
        result = self._result(
            status=SessionStatus.CANCELLED,
            termination_reason=SessionTerminationReason.CANCELLED,
        )
        assert result.cleanup_verified is True

    def test_pre_start_cancellation_has_nothing_cleaned(self):
        # Pre-start cancel: run_id is null (session.started never occurred).
        result = self._result(
            status=SessionStatus.CANCELLED,
            termination_reason=SessionTerminationReason.CANCELLED,
            run_id=None,
            cleanup_verified=False,
        )
        assert result.run_id is None
        assert result.cleanup_verified is False

    def test_pre_start_cancellation_cannot_claim_verified_cleanup(self):
        with pytest.raises(ApplicationInputError):
            self._result(
                status=SessionStatus.CANCELLED,
                termination_reason=SessionTerminationReason.CANCELLED,
                run_id=None,
                cleanup_verified=True,
            )

    def test_cleanup_failed_never_claims_verified_cleanup(self):
        with pytest.raises(ApplicationInputError):
            self._result(
                status=SessionStatus.CLEANUP_FAILED,
                termination_reason=SessionTerminationReason.CLEANUP_FAILED,
                cleanup_verified=True,
            )
        result = self._result(
            status=SessionStatus.CLEANUP_FAILED,
            termination_reason=SessionTerminationReason.CLEANUP_FAILED,
            cleanup_verified=False,
        )
        assert result.cleanup_verified is False

    @pytest.mark.parametrize("status", [SessionStatus.FAILED, SessionStatus.TIMED_OUT, SessionStatus.INTERRUPTED])
    def test_failure_terminals_allow_either_cleanup_state(self, status):
        reason = {
            SessionStatus.FAILED: SessionTerminationReason.CONTROLLER_FAILED,
            SessionStatus.TIMED_OUT: SessionTerminationReason.TIMEOUT,
            SessionStatus.INTERRUPTED: SessionTerminationReason.INTERRUPTED,
        }[status]
        self._result(status=status, termination_reason=reason, cleanup_verified=True)
        self._result(status=status, termination_reason=reason, cleanup_verified=False)

    def test_cleanup_rules_never_infer_correctness(self):
        # A succeeded operational result with verified cleanup still carries
        # no scientific outcome; SessionResult has no outcome field at all.
        result = self._result()
        assert result.status is SessionStatus.SUCCEEDED
        assert not hasattr(result, "outcome")
        assert "outcome" not in result.to_mapping()
