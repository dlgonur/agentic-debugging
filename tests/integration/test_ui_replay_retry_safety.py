"""Replay retry safety regression (repair 1, issue 3).

A replay workspace displays a recorded session that is NOT the session
the app-global retry request belongs to.  Retry must be unavailable
there: invoking it must not start the newer unrelated session, the
footer must not advertise a working replay retry, and the workspace's
retry availability check must be false for replay and for a live
workspace whose identity does not match the captured request.
"""

from __future__ import annotations

from pathlib import Path

import pytest

textual = pytest.importorskip("textual")

from agentic_debugger.application.events import (  # noqa: E402
    SESSION_EVENT_SCHEMA_VERSION,
    SessionEvent,
    SessionEventKind,
    SourceKind,
)
from agentic_debugger.application.presentation import (  # noqa: E402
    PresentationIdentity,
    initial_session_view,
)
from agentic_debugger.application.replay import SessionReplaySource  # noqa: E402
from agentic_debugger.ui.app import LocalApplicationV1  # noqa: E402
from agentic_debugger.ui.models import ReplayController  # noqa: E402
from agentic_debugger.ui.screens import (  # noqa: E402
    WorkspaceMode,
    WorkspaceScreen,
)


def make_app(tmp_path: Path) -> LocalApplicationV1:
    from agentic_debugger.application.history import HistoryStore

    app = LocalApplicationV1(history_store=HistoryStore(tmp_path))
    app._retry_invoked: list[str] = []

    def _retry() -> bool:
        request = app._live_retry_request
        if not request:
            return False
        app._retry_invoked.append(request["session_id"])
        return True

    app.retry_live_session = _retry  # type: ignore[attr-defined]
    return app


def _replay_workspace(session_id: str) -> WorkspaceScreen:
    identity = PresentationIdentity(
        task_id="curated-off-by-one-002",
        source_kind=SourceKind.OFFLINE_DEMO,
        session_id=session_id,
    )
    events = (
        SessionEvent(
            schema_version=SESSION_EVENT_SCHEMA_VERSION,
            session_id=session_id,
            task_id="curated-off-by-one-002",
            run_id=None,
            sequence=0,
            timestamp_utc="2026-08-25T12:00:00Z",
            source_kind=SourceKind.OFFLINE_DEMO,
            event_kind=SessionEventKind.SESSION_CREATED,
            controller_phase=None,
            payload={"spec_fingerprint": "a" * 64},
        ),
        SessionEvent(
            schema_version=SESSION_EVENT_SCHEMA_VERSION,
            session_id=session_id,
            task_id="curated-off-by-one-002",
            run_id="run-old",
            sequence=1,
            timestamp_utc="2026-08-25T12:00:01Z",
            source_kind=SourceKind.OFFLINE_DEMO,
            event_kind=SessionEventKind.SESSION_COMPLETED,
            controller_phase=None,
            payload={"status": "succeeded", "termination_reason": "done"},
        ),
    )
    replay = SessionReplaySource(
        events=events,
        source_kind=identity.source_kind,
        task_id=identity.task_id,
        session_id=session_id,
    )
    controller = ReplayController(replay, identity)
    return WorkspaceScreen(mode=WorkspaceMode.REPLAY, controller=controller, entry=None)


class TestReplayRetrySafety:
    def test_replay_workspace_retry_unavailable_and_never_invokes(self, tmp_path: Path) -> None:
        """A replay of an OLD session with a NEWER retry request captured
        must not start the newer session."""
        from ui_support import run_headless

        async def scenario(pilot) -> None:  # pragma: no cover - headless body
            app = pilot.app
            app._live_retry_request = {"session_id": "sess-newer-9999", "invoke": None}
            workspace = _replay_workspace("sess-old-1111")
            pilot.app.push_screen(workspace)
            await pilot.pause()
            # Check availability: replay must be gated off.
            assert workspace._retry_available() is False
            # Invoking the action must not touch the app-global retry request.
            workspace.action_retry_session()
            assert app._retry_invoked == []

        run_headless(make_app(tmp_path), scenario)

    def test_live_workspace_mismatched_identity_unavailable(self, tmp_path: Path) -> None:
        """A terminal LIVE workspace whose identity differs from the
        captured request must not retry the unrelated session."""
        from ui_support import run_headless

        async def scenario(pilot) -> None:  # pragma: no cover - headless body
            app = pilot.app
            app._live_retry_request = {"session_id": "sess-captured-2222", "invoke": None}
            identity = PresentationIdentity(
                task_id="local-project-debug",
                source_kind=SourceKind.LOCAL_PROJECT,
                session_id="sess-other-3333",
            )
            workspace = WorkspaceScreen(
                mode=WorkspaceMode.LIVE,
                identity=identity,
                view=initial_session_view(identity),
            )
            assert workspace._retry_available() is False
            workspace.action_retry_session()
            assert app._retry_invoked == []

        run_headless(make_app(tmp_path), scenario)

    def test_live_workspace_matching_identity_requires_terminal(self, tmp_path: Path) -> None:
        """A matching live workspace is still not retryable until the
        terminal result has been delivered."""
        from ui_support import run_headless

        async def scenario(pilot) -> None:  # pragma: no cover - headless body
            app = pilot.app
            captured = "sess-captured-2222"
            app._live_retry_request = {"session_id": captured, "invoke": None}
            identity = PresentationIdentity(
                task_id="local-project-debug",
                source_kind=SourceKind.LOCAL_PROJECT,
                session_id=captured,
            )
            workspace = WorkspaceScreen(
                mode=WorkspaceMode.LIVE,
                identity=identity,
                view=initial_session_view(identity),
            )
            assert workspace._retry_available() is False

        run_headless(make_app(tmp_path), scenario)
