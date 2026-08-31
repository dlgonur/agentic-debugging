"""Shared support for the Local Application V1 Textual headless tests.

Builds app-owned session directories (journal + registration) directly
through the accepted application APIs so the tests control the exact
recorded content without spawning workers, and provides a small asyncio
driver for ``App.run_test()``.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.application.events import (
    SESSION_EVENT_SCHEMA_VERSION,
    SessionEvent,
    SessionEventKind,
    SourceKind,
    validate_session_event_stream,
)
from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.journal import SessionEventJournal

VALID_TASK_ID = "curated-off-by-one-002"
VALID_RUN_ID = "run-test-001"
TIMESTAMP = "2026-08-14T08:00:00Z"


def make_event(
    kind: SessionEventKind,
    payload: dict[str, Any],
    *,
    sequence: int,
    session_id: str,
    run_id: Optional[str] = None,
    controller_phase: Optional[ControllerState] = None,
) -> SessionEvent:
    return SessionEvent.from_mapping(
        {
            "schema_version": SESSION_EVENT_SCHEMA_VERSION,
            "session_id": session_id,
            "task_id": VALID_TASK_ID,
            "run_id": run_id,
            "sequence": sequence,
            "timestamp_utc": TIMESTAMP,
            "source_kind": SourceKind.OFFLINE_DEMO.value,
            "event_kind": kind.value,
            "controller_phase": (
                controller_phase.value if controller_phase is not None else None
            ),
            "payload": payload,
        }
    )


def make_rich_stream(session_id: str) -> tuple[SessionEvent, ...]:
    """A complete enriched app-owned session stream (Task-4 vocabulary)."""
    events = [
        make_event(
            SessionEventKind.SESSION_CREATED,
            {"spec_fingerprint": "a" * 64},
            sequence=0,
            session_id=session_id,
        ),
        make_event(
            SessionEventKind.SESSION_STARTED,
            {},
            sequence=1,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.SESSION_STATUS_CHANGED,
            {"status": "running", "phase": "executing_tool"},
            sequence=2,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.CONTROLLER_TRANSITION,
            {"source_state": "Reproduce", "target_state": "Understand", "reason": "reproduced"},
            sequence=3,
            session_id=session_id,
            run_id=VALID_RUN_ID,
            controller_phase=ControllerState.UNDERSTAND,
        ),
        make_event(
            SessionEventKind.MODEL_REQUEST_STARTED,
            {"request_index": 0},
            sequence=4,
            session_id=session_id,
            run_id=VALID_RUN_ID,
            controller_phase=ControllerState.UNDERSTAND,
        ),
        make_event(
            SessionEventKind.MODEL_REQUEST_COMPLETED,
            {"request_index": 0, "status": "ok"},
            sequence=5,
            session_id=session_id,
            run_id=VALID_RUN_ID,
            controller_phase=ControllerState.UNDERSTAND,
        ),
        make_event(
            SessionEventKind.MODEL_DIRECTIVE_ACCEPTED,
            {"directive_kind": "action", "action_name": "get_stack_summary", "target_state": None},
            sequence=6,
            session_id=session_id,
            run_id=VALID_RUN_ID,
            controller_phase=ControllerState.UNDERSTAND,
        ),
        make_event(
            SessionEventKind.TOOL_STARTED,
            {"tool_name": "get_stack_summary"},
            sequence=7,
            session_id=session_id,
            run_id=VALID_RUN_ID,
            controller_phase=ControllerState.UNDERSTAND,
        ),
        make_event(
            SessionEventKind.TOOL_COMPLETED,
            {"tool_name": "get_stack_summary", "status": "ok"},
            sequence=8,
            session_id=session_id,
            run_id=VALID_RUN_ID,
            controller_phase=ControllerState.UNDERSTAND,
        ),
        make_event(
            SessionEventKind.DEBUGGER_STARTED,
            {"script": "recent_window.py", "breakpoints": ["recent_window.py:25"]},
            sequence=9,
            session_id=session_id,
            run_id=VALID_RUN_ID,
            controller_phase=ControllerState.UNDERSTAND,
        ),
        make_event(
            SessionEventKind.DEBUGGER_LOCATION_CHANGED,
            {"script": "recent_window.py", "line": 25, "function": "recent_window", "pause_generation": 1},
            sequence=10,
            session_id=session_id,
            run_id=VALID_RUN_ID,
            controller_phase=ControllerState.UNDERSTAND,
        ),
        make_event(
            SessionEventKind.DEBUGGER_STACK_OBSERVED,
            {
                "pause_generation": 1,
                "frames": [
                    {"index": 0, "function": "recent_window", "file": "recent_window.py", "line": 25, "is_current": True},
                    {"index": 1, "function": "<module>", "file": "recent_window.py", "line": 40, "is_current": False},
                ],
            },
            sequence=11,
            session_id=session_id,
            run_id=VALID_RUN_ID,
            controller_phase=ControllerState.UNDERSTAND,
        ),
        make_event(
            SessionEventKind.DEBUGGER_LOCALS_OBSERVED,
            {
                "pause_generation": 1,
                "locals": [
                    {"name": "days", "summary": "<list size=4>"},
                    {"name": "api_key", "summary": "<redacted: credential-shaped local name>"},
                ],
            },
            sequence=12,
            session_id=session_id,
            run_id=VALID_RUN_ID,
            controller_phase=ControllerState.UNDERSTAND,
        ),
        make_event(
            SessionEventKind.SOURCE_SNAPSHOT,
            {
                "path": "recent_window.py",
                "sha256": "b" * 64,
                "text": "def recent_window(days, limit):\n    return days[-limit:]\n",
                "line_count": 2,
                "truncated": False,
                "stage": "initial",
            },
            sequence=13,
            session_id=session_id,
            run_id=VALID_RUN_ID,
            controller_phase=ControllerState.UNDERSTAND,
        ),
        make_event(
            SessionEventKind.DIAGNOSIS_RECORDED,
            {"text": "empty window collapses", "file_path": "recent_window.py", "symbol": "recent_window", "confidence": "high"},
            sequence=14,
            session_id=session_id,
            run_id=VALID_RUN_ID,
            controller_phase=ControllerState.UNDERSTAND,
        ),
        make_event(
            SessionEventKind.PATCH_PROPOSED,
            {
                "attempt_index": 0,
                "patch_sha256": "c" * 64,
                "patch_text": "--- a/recent_window.py\n+++ b/recent_window.py\n@@ -1,2 +1,2 @@\n-def recent_window(days, limit):\n+def recent_window(days, limit=3):\n",
            },
            sequence=15,
            session_id=session_id,
            run_id=VALID_RUN_ID,
            controller_phase=ControllerState.PATCH,
        ),
        make_event(
            SessionEventKind.PATCH_APPLIED,
            {"attempt_index": 0, "changed_files": ["recent_window.py"], "syntax_passed": True},
            sequence=16,
            session_id=session_id,
            run_id=VALID_RUN_ID,
            controller_phase=ControllerState.PATCH,
        ),
        make_event(
            SessionEventKind.SOURCE_SNAPSHOT,
            {
                "path": "recent_window.py",
                "sha256": "d" * 64,
                "text": "def recent_window(days, limit=3):\n    return days[-limit:]\n",
                "line_count": 2,
                "truncated": False,
                "stage": "applied",
            },
            sequence=17,
            session_id=session_id,
            run_id=VALID_RUN_ID,
            controller_phase=ControllerState.PATCH,
        ),
        make_event(
            SessionEventKind.SESSION_STATUS_CHANGED,
            {"status": "running", "phase": "verifying"},
            sequence=18,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.VERIFIER_STARTED,
            {},
            sequence=19,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.VERIFIER_STAGE_STARTED,
            {"stage": "prepare_workspace"},
            sequence=20,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.VERIFIER_STAGE_COMPLETED,
            {"stage": "prepare_workspace", "status": "completed"},
            sequence=21,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.VERIFIER_STAGE_STARTED,
            {"stage": "f2p_p2p_checks"},
            sequence=22,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.VERIFIER_STAGE_COMPLETED,
            {"stage": "f2p_p2p_checks", "status": "completed"},
            sequence=23,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.VERIFIER_COMPLETED,
            {
                "status": "COMPLETED",
                "outcome": "RESOLVED",
                "f2p_passed": 1,
                "f2p_total": 1,
                "p2p_passed": 2,
                "p2p_total": 2,
                "workspace_cleaned": True,
            },
            sequence=24,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.CLEANUP_STARTED,
            {},
            sequence=25,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.CLEANUP_COMPLETED,
            {"verified": True},
            sequence=26,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.SESSION_COMPLETED,
            {"status": "succeeded", "termination_reason": "done"},
            sequence=27,
            session_id=session_id,
            run_id=VALID_RUN_ID,
        ),
    ]
    validate_session_event_stream(events)
    return tuple(events)


def renumber(events: tuple[SessionEvent, ...]) -> tuple[SessionEvent, ...]:
    """Rebuild a stream with contiguous sequences after filtering.

    ``dataclasses.replace`` cannot be used: the event's canonicalized
    payload (tuples) would be re-validated as if it were raw input.  The
    events are rebuilt through their strict public mapping round-trip.
    """
    rebuilt = []
    for index, event in enumerate(events):
        mapping = event.to_mapping()
        mapping["sequence"] = index
        rebuilt.append(SessionEvent.from_mapping(mapping))
    return tuple(rebuilt)


def populate_history(
    store: HistoryStore,
    session_id: str,
    events: Optional[tuple[SessionEvent, ...]] = None,
    *,
    corrupt: bool = False,
    interrupted: bool = False,
) -> Path:
    """Create one app-owned session directory and register it.

    ``corrupt`` writes a malformed journal line; ``interrupted`` drops the
    terminal events (a valid readable prefix).  Returns the session dir.
    """
    session_dir = store.session_dir(session_id)
    session_dir.mkdir(parents=True)
    journal_path = session_dir / "session.events.jsonl"
    if corrupt:
        journal_path.write_text(
            "{not-json}\n", encoding="utf-8"
        )
        return session_dir
    if events is None:
        events = make_rich_stream(session_id)
    if interrupted:
        events = tuple(event for event in events if event.event_kind not in (
            SessionEventKind.CLEANUP_STARTED,
            SessionEventKind.CLEANUP_COMPLETED,
            SessionEventKind.SESSION_COMPLETED,
            SessionEventKind.SESSION_FAILED,
            SessionEventKind.SESSION_CANCELLED,
        ))
    journal = SessionEventJournal(
        journal_path,
        session_id=session_id,
        task_id=VALID_TASK_ID,
        source_kind=SourceKind.OFFLINE_DEMO,
    )
    for event in events:
        journal.append(event)
    journal.close()
    try:
        store.register(session_dir)
    except Exception:
        pass
    return session_dir


def run_headless(
    app: Any,
    actions: Callable[[Any], Awaitable[None]],
    *,
    size: tuple[int, int] = (120, 40),
) -> None:
    """Run one headless app scenario with ``App.run_test`` + Pilot.

    Ensures headless review and test rendering cannot mutate the operator's
    default configuration directory if executed outside an isolated harness.
    """
    temp_dir: Optional[tempfile.TemporaryDirectory[str]] = None
    if "AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH" not in os.environ and "AGENTIC_DEBUGGER_CONFIG_DIR" not in os.environ:
        temp_dir = tempfile.TemporaryDirectory(prefix="agentic_debugger_headless_")
        os.environ["AGENTIC_DEBUGGER_CONFIG_DIR"] = temp_dir.name
        os.environ["AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH"] = str(
            Path(temp_dir.name) / "provider-configurations.json"
        )

    try:
        async def _run() -> None:
            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                await actions(pilot)
                await pilot.pause()

        asyncio.run(_run())
    finally:
        if temp_dir is not None:
            os.environ.pop("AGENTIC_DEBUGGER_CONFIG_DIR", None)
            os.environ.pop("AGENTIC_DEBUGGER_PROVIDER_CONFIG_PATH", None)
            temp_dir.cleanup()
