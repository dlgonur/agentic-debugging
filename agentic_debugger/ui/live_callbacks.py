"""Live-runner callback coordination for the terminal application.

This module owns the live-session callback family operating on the
APPLICATION passed explicitly (``LocalApplicationV1`` remains the
application lifecycle authority): runner-thread -> event-loop marshalling,
live events/liveness/terminal/failure UI updates, manual retry, bounded
auto-retry, and runner ownership release.  The corresponding methods on
``LocalApplicationV1`` delegate here.
"""

from __future__ import annotations

from typing import Optional, Tuple

from agentic_debugger.application.events import (
    SessionEvent,
    SessionStatus,
    SessionTerminationReason,
)
from agentic_debugger.application.live_execution import (
    EphemeralSnapshot,
    KnownCeilings,
    LiveExecutionState,
    project_live_execution,
)
from agentic_debugger.application.presentation import reduce_event
from agentic_debugger.application.session import SessionResult
from agentic_debugger.application.worker_protocol import WorkerLiveness
from agentic_debugger.ui.screens import HomeScreen


_AUTO_RETRY_TERMINALS: frozenset[
    tuple[SessionStatus, SessionTerminationReason]
] = frozenset(
    {
        (SessionStatus.FAILED, SessionTerminationReason.MODEL_ERROR),
        (SessionStatus.FAILED, SessionTerminationReason.CONTROLLER_FAILED),
        (SessionStatus.FAILED, SessionTerminationReason.DIRECTIVE_EXHAUSTED),
        (SessionStatus.TIMED_OUT, SessionTerminationReason.TIMEOUT),
    }
)
def on_live_started(app) -> None:
    try:
        app.call_from_thread(app._live_started_ui)
    except Exception:
        pass


def live_started_ui(app) -> None:
    workspace = app._live_workspace
    if workspace is not None and workspace.is_mounted:
        workspace.refresh_live()


def on_live_events(app, events: Tuple[SessionEvent, ...]) -> None:
    try:
        app.call_from_thread(app._live_events_ui, events)
    except Exception:
        pass


def live_events_ui(app, events: Tuple[SessionEvent, ...]) -> None:
    if app._live_view is None:
        return
    for event in events:
        if event.sequence <= app._live_last_sequence:
            continue
        app._live_last_sequence = event.sequence
        app._live_view = reduce_event(app._live_view, event)
        # A journal prefix is already retained by the worker and reduced
        # into the bounded timeline. Keep only a bounded compatibility
        # tail; never create a second unbounded event log in the app.
        app._live_events = (app._live_events + (event,))[-2000:]
    workspace = app._live_workspace
    if workspace is not None and workspace.is_mounted:
        workspace.refresh_live()


def on_live_liveness(
    self, generation: int, session_id: str, liveness: WorkerLiveness
) -> None:
    try:
        app.call_from_thread(app._live_liveness_ui, generation, session_id, liveness)
    except Exception:
        pass


def live_liveness_ui(
    self, generation: int, session_id: str, liveness: WorkerLiveness
) -> None:
    if (
        app._live_view is None
        or app._live_view.status.terminal
        or generation != app._live_generation
        or app._live_identity is None
        or app._live_identity.session_id != session_id
    ):
        return
    app._live_snapshot = EphemeralSnapshot(
        generation=app._live_generation,
        request_index=liveness.request_index,
        request_elapsed_seconds=liveness.request_elapsed_seconds,
        last_activity_age_seconds=liveness.last_activity_age_seconds,
        transport_alive=liveness.transport_alive,
        watchdog_idle_seconds=liveness.watchdog_idle_seconds,
        received_monotonic=time.monotonic(),
    )
    workspace = app._live_workspace
    if workspace is not None and workspace.is_mounted:
        workspace.refresh_live()


def retry_live_session(app) -> bool:
    """Restart the most recent retryable live session with identical
    parameters, linked to the original session in the journal.

    A manual retry is a single explicit attempt: it starts with a
    zero auto-retry budget, so it can never mint a fresh auto-retry
    chain.  Returns False when a session is active or no retryable
    start request is captured.  The re-start re-validates everything
    (model availability, project cleanliness); a changed environment
    fails closed instead of silently degrading.
    """
    if app._live_runner is not None:
        return False
    request = app._live_retry_request
    if not request:
        return False
    original = request["session_id"]
    app._live_retry_request = None
    try:
        # remaining=0: a manual retry is one explicit attempt and can
        # never start another automatic chain.
        request["invoke"](original, remaining=0)
        return True
    except Exception as exc:
        app.notify(f"Retry failed: {exc}", severity="error", title="Retry")
        return False


def maybe_auto_retry(app, result: object) -> None:
    """Start one linked retry when the terminal failure is retryable.

    Retryable failures are transient or model-capability failures where
    a fresh attempt can genuinely succeed: transport/provider errors,
    timeouts, controller crashes, and directive exhaustion.  The
    worker's real timeout terminal is ``TIMED_OUT`` + ``TIMEOUT``, so
    eligibility is the exact status/reason pair
    (``_AUTO_RETRY_TERMINALS``), never the reason alone.  User
    cancellations, interrupts, cleanup failures, honest unresolved
    verifier outcomes, and any inconsistent status/reason combination
    fail closed.
    """
    if app._live_auto_retry_budget <= 0 or not isinstance(result, SessionResult):
        return
    if (result.status, result.termination_reason) not in _AUTO_RETRY_TERMINALS:
        return
    app._live_auto_retry_budget -= 1
    request = app._live_retry_request
    if not request:
        return
    original = request["session_id"]
    remaining = app._live_auto_retry_budget
    app._live_retry_request = None
    try:
        request["invoke"](original, remaining=remaining)
        app.notify(
            f"Session failed ({result.termination_reason.value}); "
            f"auto-retrying ({remaining} attempt(s) remaining).",
            severity="warning",
            title="Auto-retry",
        )
    except Exception as exc:
        app.notify(f"Auto-retry failed: {exc}", severity="error", title="Auto-retry")


def on_live_terminal(app, result: SessionResult, registration_error: Optional[str]) -> None:
    try:
        app.call_from_thread(app._live_terminal_ui, result, registration_error)
    except Exception:
        pass


def live_terminal_ui(app, result: object, registration_error: Optional[str]) -> None:
    workspace = app._live_workspace
    if workspace is not None:
        # The workspace records the terminal itself (its ``is_mounted``
        # guard handles a fast worker that finished before the mount).
        workspace.show_live_terminal(result, registration_error)
    # The terminal has been delivered: the runner is finished (its own
    # supervision thread closes the worker right after this callback), so
    # the app no longer considers it active and another session may start.
    app._release_live_runner()
    app._maybe_auto_retry(result)
    home = app.screen
    if isinstance(home, HomeScreen):
        home.refresh_history()


def on_live_failure(app, diagnostic: str) -> None:
    try:
        app.call_from_thread(app._live_failure_ui, diagnostic)
    except Exception:
        pass


def live_failure_ui(app, diagnostic: str) -> None:
    workspace = app._live_workspace
    if workspace is not None:
        workspace.show_live_failure(diagnostic)
    else:
        app.notify(diagnostic, severity="error", title="Live session")
    # A startup/supervision failure is terminal for the runner: release
    # ownership so a retry can start another session.  The runner's own
    # supervision path performs the final worker handle close.
    #
    # This path intentionally has no SessionResult and therefore no
    # terminal status/reason pair, so it never reaches
    # ``_maybe_auto_retry`` (which only speaks the terminal contract);
    # the captured retry request stays armed and the failure is
    # manual-retry-only (``r``).  No synthetic terminal is invented.
    app._release_live_runner()


def release_live_runner(app) -> None:
    """Drop application ownership of a finished/failed live runner.

    Only the runner's own supervision thread closes the worker, so this
    never joins the runner thread from the event loop (that would
    deadlock the terminal callback, which the driver thread is waiting
    on).  The recorded presentation data (``live_view``/``live_events``)
    stays available for reopening and replay parity.
    """
    app._live_runner = None
    app._live_workspace = None
