"""Export 5 real rendered Textual SVG evidence screenshots for live-execution-trace review."""

from __future__ import annotations

import asyncio
from pathlib import Path

from agentic_debugger.application.events import (
    SESSION_EVENT_SCHEMA_VERSION,
    SessionEvent,
    SessionEventKind,
    SourceKind,
)
from agentic_debugger.application.presentation import (
    PresentationIdentity,
    initial_session_view,
    reduce_event,
)
from agentic_debugger.application.replay import SessionReplaySource
from agentic_debugger.ui.app import LocalApplicationV1
from agentic_debugger.ui.models import ReplayController
from agentic_debugger.ui.screens import WorkspaceMode, WorkspaceScreen


OUTPUT_DIR = Path("_ai-review/live-execution-trace")


def _make_event(
    kind: SessionEventKind,
    payload: dict,
    sequence: int,
    *,
    timestamp_utc: str = "2026-08-30T10:00:00Z",
    session_id: str = "sess.evidence",
    task_id: str = "curated-off-by-one-002",
) -> SessionEvent:
    return SessionEvent.from_mapping(
        {
            "schema_version": SESSION_EVENT_SCHEMA_VERSION,
            "event_kind": kind.value,
            "payload": payload,
            "sequence": sequence,
            "session_id": session_id,
            "task_id": task_id,
            "run_id": None if kind is SessionEventKind.SESSION_CREATED else "run-001",
            "timestamp_utc": timestamp_utc,
            "source_kind": SourceKind.OFFLINE_DEMO.value,
            "controller_phase": None,
        }
    )


def _build_active_events() -> list[SessionEvent]:
    return [
        _make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, 0, timestamp_utc="2026-08-30T10:00:00Z"),
        _make_event(SessionEventKind.SESSION_STARTED, {}, 1, timestamp_utc="2026-08-30T10:00:00Z"),
        _make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"}, 2, timestamp_utc="2026-08-30T10:00:01Z"),
        _make_event(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": 0}, 3, timestamp_utc="2026-08-30T10:00:01Z"),
        _make_event(SessionEventKind.MODEL_DIRECTIVE_ACCEPTED, {"action_name": "get_source_window", "directive_kind": "action", "target_state": None}, 4, timestamp_utc="2026-08-30T10:00:02Z"),
        _make_event(SessionEventKind.MODEL_REQUEST_COMPLETED, {"request_index": 0, "status": "ok"}, 5, timestamp_utc="2026-08-30T10:00:03Z"),
        _make_event(SessionEventKind.TOOL_STARTED, {"tool_name": "get_source_window", "target": "cookiecutter/config.py:40-80"}, 6, timestamp_utc="2026-08-30T10:00:03Z"),
        _make_event(SessionEventKind.TOOL_COMPLETED, {"tool_name": "get_source_window", "status": "ok"}, 7, timestamp_utc="2026-08-30T10:00:04Z"),
        _make_event(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": 1}, 8, timestamp_utc="2026-08-30T10:00:04Z"),
        _make_event(SessionEventKind.MODEL_DIRECTIVE_ACCEPTED, {"action_name": "set_breakpoint", "directive_kind": "action", "target_state": None}, 9, timestamp_utc="2026-08-30T10:00:05Z"),
        _make_event(SessionEventKind.MODEL_REQUEST_COMPLETED, {"request_index": 1, "status": "ok"}, 10, timestamp_utc="2026-08-30T10:00:06Z"),
        _make_event(SessionEventKind.DEBUGGER_STARTED, {"script": "cookiecutter/config.py", "breakpoints": ["cookiecutter/config.py:54"]}, 11, timestamp_utc="2026-08-30T10:00:06Z"),
        _make_event(SessionEventKind.DEBUGGER_STACK_OBSERVED, {"pause_generation": 1, "frames": []}, 12, timestamp_utc="2026-08-30T10:00:07Z"),
        _make_event(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": 2}, 13, timestamp_utc="2026-08-30T10:00:07Z"),
        _make_event(SessionEventKind.TOOL_STARTED, {"tool_name": "eval_expression", "target": "val is None"}, 14, timestamp_utc="2026-08-30T10:00:08Z"),
    ]


def _build_recoverable_events() -> list[SessionEvent]:
    return [
        _make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, 0, timestamp_utc="2026-08-30T10:00:00Z"),
        _make_event(SessionEventKind.SESSION_STARTED, {}, 1, timestamp_utc="2026-08-30T10:00:00Z"),
        _make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"}, 2, timestamp_utc="2026-08-30T10:00:01Z"),
        _make_event(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": 0}, 3, timestamp_utc="2026-08-30T10:00:01Z"),
        _make_event(SessionEventKind.MODEL_DIRECTIVE_ACCEPTED, {"action_name": "apply_patch", "directive_kind": "action", "target_state": None}, 4, timestamp_utc="2026-08-30T10:00:02Z"),
        _make_event(SessionEventKind.MODEL_REQUEST_COMPLETED, {"request_index": 0, "status": "ok"}, 5, timestamp_utc="2026-08-30T10:00:03Z"),
        _make_event(SessionEventKind.PATCH_PROPOSED, {"attempt_index": 0, "patch_sha256": "1" * 64, "patch_text": "--- a/cookiecutter/config.py\n+++ b/cookiecutter/config.py\n@@ -54,2 +54,2 @@\n-    return None\n+    return\n"}, 6, timestamp_utc="2026-08-30T10:00:04Z"),
        _make_event(SessionEventKind.PATCH_APPLY_FAILED, {"attempt_index": 0, "apply_failure_reason": "syntax_error"}, 7, timestamp_utc="2026-08-30T10:00:04Z"),
        _make_event(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": 1}, 8, timestamp_utc="2026-08-30T10:00:05Z"),
        _make_event(SessionEventKind.MODEL_DIRECTIVE_ACCEPTED, {"action_name": "apply_patch", "directive_kind": "action", "target_state": None}, 9, timestamp_utc="2026-08-30T10:00:06Z"),
        _make_event(SessionEventKind.MODEL_REQUEST_COMPLETED, {"request_index": 1, "status": "ok"}, 10, timestamp_utc="2026-08-30T10:00:07Z"),
        _make_event(SessionEventKind.PATCH_PROPOSED, {"attempt_index": 1, "patch_sha256": "2" * 64, "patch_text": "--- a/cookiecutter/config.py\n+++ b/cookiecutter/config.py\n@@ -54,2 +54,2 @@\n-    return None\n+    return \"\"\n"}, 11, timestamp_utc="2026-08-30T10:00:08Z"),
        _make_event(SessionEventKind.PATCH_APPLIED, {"attempt_index": 1, "changed_files": ["cookiecutter/config.py"], "syntax_passed": True}, 12, timestamp_utc="2026-08-30T10:00:08Z"),
        _make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "verifying"}, 13, timestamp_utc="2026-08-30T10:00:09Z"),
        _make_event(SessionEventKind.VERIFIER_STARTED, {}, 14, timestamp_utc="2026-08-30T10:00:09Z"),
        _make_event(SessionEventKind.VERIFIER_COMPLETED, {
            "status": "COMPLETED",
            "outcome": "RESOLVED",
            "f2p_passed": 1,
            "f2p_total": 1,
            "p2p_passed": 12,
            "p2p_total": 12,
            "workspace_cleaned": True,
        }, 15, timestamp_utc="2026-08-30T10:00:15Z"),
        _make_event(SessionEventKind.CLEANUP_STARTED, {}, 16, timestamp_utc="2026-08-30T10:00:15Z"),
        _make_event(SessionEventKind.CLEANUP_COMPLETED, {"verified": True}, 17, timestamp_utc="2026-08-30T10:00:16Z"),
        _make_event(SessionEventKind.SESSION_COMPLETED, {"status": "succeeded", "termination_reason": "done"}, 18, timestamp_utc="2026-08-30T10:00:16Z"),
    ]


def _build_completed_events() -> list[SessionEvent]:
    return [
        _make_event(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": "a" * 64}, 0, timestamp_utc="2026-08-30T10:00:00Z"),
        _make_event(SessionEventKind.SESSION_STARTED, {}, 1, timestamp_utc="2026-08-30T10:00:00Z"),
        _make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "executing_tool"}, 2, timestamp_utc="2026-08-30T10:00:01Z"),
        _make_event(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": 0}, 3, timestamp_utc="2026-08-30T10:00:01Z"),
        _make_event(SessionEventKind.MODEL_DIRECTIVE_ACCEPTED, {"action_name": "get_source_window", "directive_kind": "action", "target_state": None}, 4, timestamp_utc="2026-08-30T10:00:02Z"),
        _make_event(SessionEventKind.MODEL_REQUEST_COMPLETED, {"request_index": 0, "status": "ok"}, 5, timestamp_utc="2026-08-30T10:00:04Z"),
        _make_event(SessionEventKind.TOOL_STARTED, {"tool_name": "get_source_window", "target": "cookiecutter/config.py:40-80"}, 6, timestamp_utc="2026-08-30T10:00:04Z"),
        _make_event(SessionEventKind.TOOL_COMPLETED, {"tool_name": "get_source_window", "status": "ok"}, 7, timestamp_utc="2026-08-30T10:00:05Z"),
        _make_event(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": 1}, 8, timestamp_utc="2026-08-30T10:00:05Z"),
        _make_event(SessionEventKind.MODEL_DIRECTIVE_ACCEPTED, {"action_name": "apply_patch", "directive_kind": "action", "target_state": None}, 9, timestamp_utc="2026-08-30T10:00:08Z"),
        _make_event(SessionEventKind.MODEL_REQUEST_COMPLETED, {"request_index": 1, "status": "ok"}, 10, timestamp_utc="2026-08-30T10:00:10Z"),
        _make_event(SessionEventKind.PATCH_PROPOSED, {"attempt_index": 0, "patch_sha256": "0" * 64, "patch_text": "--- a/cookiecutter/config.py\n+++ b/cookiecutter/config.py\n@@ -54,2 +54,2 @@\n-    return None\n+    return \"\"\n"}, 11, timestamp_utc="2026-08-30T10:00:10Z"),
        _make_event(SessionEventKind.PATCH_APPLIED, {"attempt_index": 0, "changed_files": ["cookiecutter/config.py"], "syntax_passed": True}, 12, timestamp_utc="2026-08-30T10:00:11Z"),
        _make_event(SessionEventKind.SESSION_STATUS_CHANGED, {"status": "running", "phase": "verifying"}, 13, timestamp_utc="2026-08-30T10:00:11Z"),
        _make_event(SessionEventKind.VERIFIER_STARTED, {}, 14, timestamp_utc="2026-08-30T10:00:11Z"),
        _make_event(SessionEventKind.VERIFIER_COMPLETED, {
            "status": "COMPLETED",
            "outcome": "RESOLVED",
            "f2p_passed": 1,
            "f2p_total": 1,
            "p2p_passed": 12,
            "p2p_total": 12,
            "workspace_cleaned": True,
        }, 15, timestamp_utc="2026-08-30T10:00:25Z"),
        _make_event(SessionEventKind.CLEANUP_STARTED, {}, 16, timestamp_utc="2026-08-30T10:00:25Z"),
        _make_event(SessionEventKind.CLEANUP_COMPLETED, {"verified": True}, 17, timestamp_utc="2026-08-30T10:00:26Z"),
        _make_event(SessionEventKind.SESSION_COMPLETED, {"status": "succeeded", "termination_reason": "done"}, 18, timestamp_utc="2026-08-30T10:00:26Z"),
    ]


async def capture_scenarios():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    identity = PresentationIdentity(
        task_id="curated-off-by-one-002",
        source_kind=SourceKind.OFFLINE_DEMO,
        session_id="sess.evidence",
    )

    # 1. Active mid-session live trace
    active_events = _build_active_events()
    active_view = initial_session_view(identity)
    for ev in active_events:
        active_view = reduce_event(active_view, ev)

    app1 = LocalApplicationV1()
    async with app1.run_test(size=(120, 36)) as pilot:
        ws = WorkspaceScreen(mode=WorkspaceMode.LIVE, identity=identity, view=active_view)
        ws._live_events = tuple(active_events)
        ws._live_last_sequence = active_events[-1].sequence
        await app1.push_screen(ws)
        await pilot.pause()
        svg = app1.export_screenshot(title="01 - Active Mid-Session Live Trace")
        (OUTPUT_DIR / "01-active-live-trace.svg").write_text(svg, encoding="utf-8")
        print("Exported 01-active-live-trace.svg")

    # 2. Recoverable patch loop -> retry -> verification
    rec_events = _build_recoverable_events()
    source_rec = SessionReplaySource(
        events=tuple(rec_events),
        source_kind=SourceKind.OFFLINE_DEMO,
        task_id="curated-off-by-one-002",
        session_id="sess.evidence",
    )
    ctrl_rec = ReplayController(source_rec, identity)
    ctrl_rec.seek(len(rec_events))

    app2 = LocalApplicationV1()
    async with app2.run_test(size=(120, 36)) as pilot:
        ws = WorkspaceScreen(mode=WorkspaceMode.REPLAY, controller=ctrl_rec)
        await app2.push_screen(ws)
        await pilot.pause()
        svg = app2.export_screenshot(title="02 - Recoverable Patch Loop & Verification")
        (OUTPUT_DIR / "02-recoverable-patch-loop.svg").write_text(svg, encoding="utf-8")
        print("Exported 02-recoverable-patch-loop.svg")

    # 3. Completed Live session
    comp_events = _build_completed_events()
    source_comp = SessionReplaySource(
        events=tuple(comp_events),
        source_kind=SourceKind.OFFLINE_DEMO,
        task_id="curated-off-by-one-002",
        session_id="sess.evidence",
    )
    ctrl_comp = ReplayController(source_comp, identity)
    ctrl_comp.seek(len(comp_events))

    app3 = LocalApplicationV1()
    async with app3.run_test(size=(120, 36)) as pilot:
        ws = WorkspaceScreen(mode=WorkspaceMode.REPLAY, controller=ctrl_comp)
        await app3.push_screen(ws)
        await pilot.pause()
        svg = app3.export_screenshot(title="03 - Completed Live Session Trace")
        (OUTPUT_DIR / "03-completed-live-session.svg").write_text(svg, encoding="utf-8")
        print("Exported 03-completed-live-session.svg")

    # 4. Redesigned timing-only Timeline tab
    app4 = LocalApplicationV1()
    async with app4.run_test(size=(120, 36)) as pilot:
        ws = WorkspaceScreen(mode=WorkspaceMode.REPLAY, controller=ctrl_comp)
        await app4.push_screen(ws)
        await pilot.pause()
        await pilot.press("7")  # Switch to Timeline tab
        await pilot.pause()
        svg = app4.export_screenshot(title="04 - Redesigned Timing-Only Timeline")
        (OUTPUT_DIR / "04-timing-breakdown-timeline.svg").write_text(svg, encoding="utf-8")
        print("Exported 04-timing-breakdown-timeline.svg")

    # 5. Representative compact terminal (80x24)
    app5 = LocalApplicationV1()
    async with app5.run_test(size=(80, 24)) as pilot:
        ws = WorkspaceScreen(mode=WorkspaceMode.LIVE, identity=identity, view=active_view)
        ws._live_events = tuple(active_events)
        ws._live_last_sequence = active_events[-1].sequence
        await app5.push_screen(ws)
        await pilot.pause()
        svg = app5.export_screenshot(title="05 - Compact Terminal Live Trace (80x24)")
        (OUTPUT_DIR / "05-compact-terminal-live.svg").write_text(svg, encoding="utf-8")
        print("Exported 05-compact-terminal-live.svg")


if __name__ == "__main__":
    asyncio.run(capture_scenarios())
