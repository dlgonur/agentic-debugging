"""Shared factories for the application-contract test suites."""

from __future__ import annotations

from agentic_debugger.application.events import (
    SESSION_EVENT_SCHEMA_VERSION,
    SessionEvent,
    SessionEventKind,
    SourceKind,
)
from agentic_debugger.application.session import SessionBudgets, SessionSpec
from agentic_debugger.application.sources import ExecutionSourceSpec

VALID_SESSION_ID = "session-test-001"
VALID_TASK_ID = "curated-off-by-one-002"
VALID_RUN_ID = "run-test-001"
VALID_SPEC_FINGERPRINT = "a" * 64
VALID_PATCH_SHA256 = "b" * 64
VALID_TIMESTAMP = "2026-08-14T08:00:00Z"

#: One valid payload mapping per event kind (the shared contract fixture).
VALID_PAYLOADS = {
    SessionEventKind.SESSION_CREATED: {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
    SessionEventKind.SESSION_STARTED: {},
    SessionEventKind.SESSION_STATUS_CHANGED: {"status": "running", "phase": "waiting_model"},
    SessionEventKind.SESSION_CANCEL_REQUESTED: {},
    SessionEventKind.SESSION_COMPLETED: {"status": "succeeded", "termination_reason": "done"},
    SessionEventKind.SESSION_FAILED: {"status": "failed", "termination_reason": "controller_failed"},
    SessionEventKind.SESSION_CANCELLED: {"status": "cancelled", "termination_reason": "cancelled"},
    SessionEventKind.CONTROLLER_STEP: {
        "step_index": 3,
        "directive_kind": "action",
        "stop_reason": None,
    },
    SessionEventKind.CONTROLLER_TRANSITION: {
        "source_state": "Reproduce",
        "target_state": "Understand",
        "reason": "moved to understanding",
    },
    SessionEventKind.MODEL_REQUEST_STARTED: {"request_index": 0},
    SessionEventKind.MODEL_REQUEST_COMPLETED: {"request_index": 0, "status": "ok"},
    SessionEventKind.MODEL_DIRECTIVE_ACCEPTED: {
        "directive_kind": "action",
        "action_name": "apply_patch",
        "target_state": None,
    },
    SessionEventKind.MODEL_DIRECTIVE_REJECTED: {
        "directive_kind": "action",
        "rejection_category": "invalid_arguments",
    },
    SessionEventKind.MODEL_CONFIGURED: {
        "profile_id": "local-dummy",
        "config_fingerprint": VALID_SPEC_FINGERPRINT,
        "display_name": "Dummy command model",
        "protocol_version": "1.3",
        "tool_version": "live-command-v1",
    },
    SessionEventKind.TOOL_STARTED: {"tool_name": "apply_patch"},
    SessionEventKind.TOOL_COMPLETED: {"tool_name": "apply_patch", "status": "ok"},
    SessionEventKind.DEBUGGER_STARTED: {
        "script": "buggy.py",
        "breakpoints": ["buggy.py:12"],
    },
    SessionEventKind.DEBUGGER_LOCATION_CHANGED: {
        "script": "buggy.py",
        "line": 12,
        "function": "main",
        "pause_generation": 1,
    },
    SessionEventKind.DEBUGGER_STACK_OBSERVED: {
        "pause_generation": 1,
        "frames": [
            {"index": 0, "function": "main", "file": "buggy.py", "line": 12, "is_current": True}
        ],
    },
    SessionEventKind.DEBUGGER_LOCALS_OBSERVED: {
        "pause_generation": 1,
        "locals": [{"name": "count", "summary": "3"}],
    },
    SessionEventKind.PATCH_PROPOSED: {"attempt_index": 0, "patch_sha256": VALID_PATCH_SHA256},
    SessionEventKind.PATCH_REJECTED: {"attempt_index": 0, "rejection_reason": "malformed_diff"},
    SessionEventKind.PATCH_APPLY_FAILED: {
        "attempt_index": 0,
        "apply_failure_reason": "hunk does not apply",
    },
    SessionEventKind.PATCH_APPLIED: {
        "attempt_index": 0,
        "changed_files": ["buggy.py"],
        "syntax_passed": True,
    },
    SessionEventKind.PATCH_REVERTED: {"attempt_index": 0},
    SessionEventKind.SOURCE_SNAPSHOT: {
        "path": "buggy.py",
        "sha256": VALID_PATCH_SHA256,
        "text": "def main():\n    return 1\n",
        "line_count": 2,
        "truncated": False,
        "stage": "initial",
    },
    SessionEventKind.DIAGNOSIS_RECORDED: {
        "text": "count is off by one",
        "file_path": "buggy.py",
        "symbol": "main",
        "confidence": "medium",
    },
    SessionEventKind.VERIFIER_STARTED: {},
    SessionEventKind.VERIFIER_STAGE_STARTED: {"stage": "prepare_workspace"},
    SessionEventKind.VERIFIER_STAGE_COMPLETED: {"stage": "prepare_workspace", "status": "completed"},
    SessionEventKind.VERIFIER_COMPLETED: {
        "status": "COMPLETED",
        "outcome": "RESOLVED",
        "f2p_passed": 1,
        "f2p_total": 1,
        "p2p_passed": 2,
        "p2p_total": 2,
        "workspace_cleaned": True,
    },
    SessionEventKind.CLEANUP_STARTED: {},
    SessionEventKind.CLEANUP_COMPLETED: {"verified": True},
    SessionEventKind.ARTIFACT_WRITTEN: {"path": "session.events.jsonl", "sha256": VALID_PATCH_SHA256},
}


def make_event(
    kind: SessionEventKind,
    payload=None,
    *,
    sequence: int = 0,
    session_id: str = VALID_SESSION_ID,
    task_id: str = VALID_TASK_ID,
    run_id=None,
    source_kind=SourceKind.OFFLINE_DEMO,
    controller_phase=None,
    timestamp: str = VALID_TIMESTAMP,
) -> SessionEvent:
    """Build one validated SessionEvent from a payload mapping."""
    if payload is None:
        payload = {}
    return SessionEvent.from_mapping(
        {
            "schema_version": SESSION_EVENT_SCHEMA_VERSION,
            "session_id": session_id,
            "task_id": task_id,
            "run_id": run_id,
            "sequence": sequence,
            "timestamp_utc": timestamp,
            "source_kind": (
                source_kind.value if isinstance(source_kind, SourceKind) else source_kind
            ),
            "event_kind": (
                kind.value if isinstance(kind, SessionEventKind) else kind
            ),
            "controller_phase": (
                controller_phase.value if controller_phase is not None else None
            ),
            "payload": payload,
        }
    )


def make_spec(
    task_id: str = VALID_TASK_ID,
    *,
    kind: SourceKind = SourceKind.OFFLINE_DEMO,
    policy: str = "static-baseline",
    model_config_ref: str | None = None,
    budgets: SessionBudgets | None = None,
    artifact_destination: str | None = None,
) -> SessionSpec:
    """Build one valid SessionSpec over the offline demo source."""
    return SessionSpec(
        task_id=task_id,
        source=ExecutionSourceSpec(
            kind=kind,
            task_id=task_id,
            policy=policy,
            model_config_ref=model_config_ref,
        ),
        budgets=budgets if budgets is not None else SessionBudgets(),
        artifact_destination=artifact_destination,
    )


def make_completed_stream() -> tuple[SessionEvent, ...]:
    """A complete happy-path stream: created -> started -> running -> cleanup -> succeeded."""
    return (
        make_event(
            SessionEventKind.SESSION_CREATED,
            {"spec_fingerprint": VALID_SPEC_FINGERPRINT},
            sequence=0,
        ),
        make_event(
            SessionEventKind.SESSION_STARTED,
            {},
            sequence=1,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.SESSION_STATUS_CHANGED,
            {"status": "running", "phase": "waiting_model"},
            sequence=2,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.CLEANUP_STARTED,
            {},
            sequence=3,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.CLEANUP_COMPLETED,
            {"verified": True},
            sequence=4,
            run_id=VALID_RUN_ID,
        ),
        make_event(
            SessionEventKind.SESSION_COMPLETED,
            {"status": "succeeded", "termination_reason": "done"},
            sequence=5,
            run_id=VALID_RUN_ID,
        ),
    )
