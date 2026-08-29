"""Regression gates for the auto-retry chain budget (repair 2).

These gates run the REAL production retry path on ``LocalApplicationV1``:

- ``start_local_project_session`` — the only start that arms an
  automatic chain and captures the retry request;
- ``_maybe_auto_retry`` — driven in the production terminal order
  (release ownership, then maybe retry, mirroring ``_live_terminal_ui``);
- ``retry_live_session`` — the manual ``r`` action.

Only expensive boundaries are replaced: repository validation, worktree
creation, worker construction, runner construction/start, screen
mounting, and notifications.  The retry-budget logic itself is never
stubbed, subclassed, or reimplemented, so every observed
``auto_retries`` value is a value the real production chain delivered.

Observable contract:

- ``auto_retries=N`` => initial attempt + at most N automatic retries,
  and the start budgets are exactly ``N, N-1, ..., 0``;
- a manual retry starts exactly one new attempt with ``auto_retries=0``
  and can never mint a fresh automatic chain;
- non-retryable terminal outcomes produce zero automatic retries.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.application.events import (  # noqa: E402
    SessionStatus,
    SessionTerminationReason,
)
from agentic_debugger.application.session import (  # noqa: E402
    SessionId,
    SessionResult,
)
from agentic_debugger.ui import app as app_module  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "tests" / "unit"))
from application_support import VALID_RUN_ID, make_spec  # noqa: E402


# Terminal status follows the real worker mapping (_terminal_for):
# FAILED-status terminals carry MODEL_ERROR / CONTROLLER_FAILED /
# DIRECTIVE_EXHAUSTED; cancellations, interruptions, unresolved outcomes
# and cleanup failures carry their own non-FAILED statuses.
_NON_FAILED_STATUS = {
    SessionTerminationReason.CANCELLED: SessionStatus.CANCELLED,
    SessionTerminationReason.INTERRUPTED: SessionStatus.INTERRUPTED,
    SessionTerminationReason.CLEANUP_FAILED: SessionStatus.CLEANUP_FAILED,
    SessionTerminationReason.UNRESOLVED: SessionStatus.UNRESOLVED,
}


def _terminal_result(session_id: str, reason: SessionTerminationReason) -> SessionResult:
    return SessionResult(
        session_id=SessionId(session_id),
        spec=make_spec(),
        status=_NON_FAILED_STATUS.get(reason, SessionStatus.FAILED),
        termination_reason=reason,
        run_id=VALID_RUN_ID,
        sequence=9,
        # cleanup_failed results cannot claim verified cleanup.
        cleanup_verified=reason is not SessionTerminationReason.CLEANUP_FAILED,
    )


@dataclass
class _Attempt:
    """One observed production start invocation."""

    session_id: str
    retry_of_session_id: Optional[str]
    budget: int  # app._live_auto_retry_budget right after this start returned


class _FakeHistoryStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def session_dir(self, session_id: str) -> Path:
        return self._root / session_id


class _FakeConfigStore:
    """Provider-resolution boundary: any profile resolves."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def get(self, profile_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            profile_id=profile_id, configuration_fingerprint="f" * 64
        )


class _RealChainHarness:
    """Drive the real LocalApplicationV1 retry chain over faked boundaries."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.app = app_module.LocalApplicationV1.__new__(app_module.LocalApplicationV1)
        self.app._live_runner = None
        self.app._live_generation = 0
        self.app._history_store = _FakeHistoryStore(tmp_path / "history")
        self.app._config_store = _FakeConfigStore(tmp_path / "config")
        self.notifications: List[str] = []
        self.app.notify = lambda *args, **kwargs: self.notifications.append(
            str(args[0]) if args else ""
        )
        self.mounted: List[SimpleNamespace] = []
        self.workspaces: List[SimpleNamespace] = []
        self.app.switch_screen = lambda screen: self.mounted.append(screen)
        self.app.push_screen = lambda screen: self.mounted.append(screen)
        self.workers: List[SimpleNamespace] = []
        self.started: List[str] = []
        self.attempts: List[_Attempt] = []
        harness = self

        class _RecordingWorker:
            def __init__(self, **kwargs) -> None:
                self.session_dir = kwargs["session_dir"]
                self.session_id = kwargs["session_id"]
                self.retry_of_session_id = kwargs.get("retry_of_session_id")
                harness.workers.append(self)

        class _RecordingRunner:
            def __init__(self, worker, **kwargs) -> None:
                self.worker = worker

            def start(self) -> None:
                harness.started.append(self.worker.session_id)

        class _RecordingWorkspace:
            def __init__(self, **kwargs) -> None:
                self.is_mounted = True
                harness.workspaces.append(self)

            def refresh_live(self) -> None:
                pass

            def show_live_terminal(self, *args) -> None:
                pass

        monkeypatch.setattr(app_module, "SessionWorkerProcess", _RecordingWorker)
        monkeypatch.setattr(app_module, "LiveSessionRunner", _RecordingRunner)
        monkeypatch.setattr(app_module, "WorkspaceScreen", _RecordingWorkspace)

        from agentic_debugger.application import local_project as local_project_module

        repo_root = tmp_path / "repo"

        def _fake_validate(project_path, launch_cwd=None):
            return SimpleNamespace(
                dirty=False, repo_root=repo_root, head_commit="a" * 40
            )

        def _fake_worktree(repo_root, head_commit):
            return SimpleNamespace(
                isolated_path=tmp_path / "isolated",
                parent_tmpdir=tmp_path / "parent-tmp",
            )

        monkeypatch.setattr(
            local_project_module, "validate_local_project", _fake_validate
        )
        monkeypatch.setattr(
            local_project_module, "create_isolated_worktree", _fake_worktree
        )
        monkeypatch.setattr(
            local_project_module, "cleanup_parent_tmpdir", lambda *args, **kwargs: None
        )

    # -- driving ------------------------------------------------------------

    def start(self, *, auto_retries: int) -> str:
        self.app.start_local_project_session(
            project_path=str(self.app._config_store.root.parent / "repo"),
            bug_description="retries must stay bounded",
            profile_id="test-profile",
            auto_retries=auto_retries,
        )
        return self.sync()

    def sync(self) -> str:
        """Record one attempt if the real start path built a new worker."""
        assert len(self.workers) > len(self.attempts), "start produced no worker"
        worker = self.workers[-1]
        self.attempts.append(
            _Attempt(
                session_id=worker.session_id,
                retry_of_session_id=worker.retry_of_session_id,
                budget=self.app._live_auto_retry_budget,
            )
        )
        return worker.session_id

    def deliver_terminal(self, result: SessionResult) -> bool:
        """Deliver a terminal in production order and report any retry.

        Mirrors ``_live_terminal_ui``: ownership is released before the
        auto-retry decision, so a linked start can create a new session.
        """
        self.app._release_live_runner()
        self.app._maybe_auto_retry(result)
        if len(self.workers) > len(self.attempts):
            self.sync()
            return True
        return False

    def run_retryable_chain(
        self, *, auto_retries: int, reason: SessionTerminationReason
    ) -> List[int]:
        self.start(auto_retries=auto_retries)
        deliveries = 0
        # Fail-closed guard: the chain must terminate on its own.  Under the
        # pre-repair default-argument bug every retry restarted with the
        # ORIGINAL budget, this loop never ended, and the guard tripped.
        while self.app._live_auto_retry_budget > 0:
            deliveries += 1
            assert deliveries <= 12, "automatic retry chain did not terminate"
            session_id = self.workers[-1].session_id
            assert self.deliver_terminal(_terminal_result(session_id, reason))
        return [attempt.budget for attempt in self.attempts]


class TestAutomaticChainBounded:
    def test_n1_budgets_exactly_1_0_and_two_attempts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _RealChainHarness(monkeypatch, tmp_path)
        budgets = harness.run_retryable_chain(
            auto_retries=1, reason=SessionTerminationReason.MODEL_ERROR
        )
        assert budgets == [1, 0]
        assert len(harness.attempts) == 2
        assert len(harness.started) == 2
        assert len(harness.mounted) == 2
        assert len(harness.workspaces) == 2
        # The chain is exhausted: a third retryable failure starts nothing.
        session_id = harness.workers[-1].session_id
        assert (
            harness.deliver_terminal(
                _terminal_result(session_id, SessionTerminationReason.MODEL_ERROR)
            )
            is False
        )
        assert len(harness.attempts) == 2

    def test_n3_budgets_exactly_3_2_1_0_and_four_attempts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _RealChainHarness(monkeypatch, tmp_path)
        budgets = harness.run_retryable_chain(
            auto_retries=3, reason=SessionTerminationReason.CONTROLLER_FAILED
        )
        assert budgets == [3, 2, 1, 0]
        assert len(harness.attempts) == 4
        session_id = harness.workers[-1].session_id
        assert (
            harness.deliver_terminal(
                _terminal_result(
                    session_id, SessionTerminationReason.CONTROLLER_FAILED
                )
            )
            is False
        )
        assert len(harness.attempts) == 4

    def test_n0_single_attempt_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _RealChainHarness(monkeypatch, tmp_path)
        harness.start(auto_retries=0)
        assert [attempt.budget for attempt in harness.attempts] == [0]
        session_id = harness.workers[-1].session_id
        assert (
            harness.deliver_terminal(
                _terminal_result(session_id, SessionTerminationReason.MODEL_ERROR)
            )
            is False
        )
        assert len(harness.attempts) == 1

    def test_each_retry_links_to_the_previous_session(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _RealChainHarness(monkeypatch, tmp_path)
        harness.run_retryable_chain(
            auto_retries=2, reason=SessionTerminationReason.MODEL_ERROR
        )
        assert len(harness.attempts) == 3
        assert harness.attempts[0].retry_of_session_id is None
        assert (
            harness.attempts[1].retry_of_session_id == harness.attempts[0].session_id
        )
        assert (
            harness.attempts[2].retry_of_session_id == harness.attempts[1].session_id
        )


class TestManualRetryBounded:
    def test_manual_retry_receives_zero_and_cannot_auto_retry(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        harness = _RealChainHarness(monkeypatch, tmp_path)
        budgets = harness.run_retryable_chain(
            auto_retries=1, reason=SessionTerminationReason.MODEL_ERROR
        )
        assert budgets == [1, 0]
        # The captured request belongs to the chain's terminal session.
        request = harness.app._live_retry_request
        assert request["session_id"] == harness.workers[-1].session_id
        # A manual retry while a session is still active is refused.
        harness.app._live_runner = object()
        assert harness.app.retry_live_session() is False
        harness.app._release_live_runner()
        # Manual r: exactly one explicit new attempt, budget zero.
        assert harness.app.retry_live_session() is True
        harness.sync()
        assert len(harness.attempts) == 3
        assert harness.attempts[-1].budget == 0
        assert (
            harness.attempts[-1].retry_of_session_id
            == harness.attempts[-2].session_id
        )
        # That manual attempt's own retryable failure cannot auto-retry.
        session_id = harness.workers[-1].session_id
        assert (
            harness.deliver_terminal(
                _terminal_result(session_id, SessionTerminationReason.MODEL_ERROR)
            )
            is False
        )
        assert len(harness.attempts) == 3


class TestNonRetryableTerminals:
    @pytest.mark.parametrize(
        "reason",
        [
            SessionTerminationReason.CANCELLED,
            SessionTerminationReason.INTERRUPTED,
            SessionTerminationReason.CLEANUP_FAILED,
            SessionTerminationReason.UNRESOLVED,
        ],
    )
    def test_no_automatic_retry(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        reason: SessionTerminationReason,
    ) -> None:
        harness = _RealChainHarness(monkeypatch, tmp_path)
        harness.start(auto_retries=3)
        session_id = harness.workers[-1].session_id
        assert harness.deliver_terminal(_terminal_result(session_id, reason)) is False
        assert len(harness.attempts) == 1
        # The untouched budget and preserved request keep the terminal
        # session manually retryable (footer contract).
        assert harness.app._live_auto_retry_budget == 3
        assert harness.app._live_retry_request["session_id"] == session_id
