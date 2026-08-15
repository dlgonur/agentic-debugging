"""INTERNAL, NON-PRODUCT worker test scenarios for the Task-3 boundary.

These scenarios exist ONLY to prove the cancellable worker boundary with
bounded deterministic executions.  They are explicitly NOT a product
execution source: Task 7 binds the real deterministic controller/demo path
into the application session lifecycle.  Nothing here may become a
user-facing source mode, and no scenario accepts executable code.

Scenario vocabulary (name -> params):

- ``synthetic_work`` — cooperative long-running execution that honors the
  cancellation token between steps;
- ``sleep_child`` — one delayed subprocess run through ``CommandRunner``
  with a cancellation checkpoint (prompt termination on cancel);
- ``pdb_session`` — a real PDB session/worker with a paused target,
  cooperative stop on cancellation;
- ``pdb_ignore_cancel`` — a real PDB worker that stays alive while the
  scenario ignores cancellation (forced-escalation descendant evidence);
- ``ignore_cancel`` — execution that ignores cooperative cancellation;
- ``ignore_cancel_with_child`` — same, owning one real descendant process;
- ``crash_hard`` — intentional hard crash (``os._exit``) with no terminal;
- ``crash`` — harness exception (complete FAILED terminal);
- ``cleanup_failure`` — forces the worker cleanup cycle to fail;
- ``break_journal`` — breaks the session journal out from under the worker;
- ``emit_large_event`` — one valid event well above 64 KiB through the
  durable journal;
- ``emit_enriched_stream`` — a bounded representative Task-4 enriched prefix
  (debugger/source/patch/diagnosis/verifier events) through the journal.

Scenarios validate their own parameters fail-closed and raise
:class:`ScenarioInputError` on malformed input.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from agentic_debugger.application import ApplicationInputError
from agentic_debugger.application.emitter import SessionEventEmitter
from agentic_debugger.application.journal import SessionEventJournal
from agentic_debugger.cancellation import CancellationError, CancellationToken
from agentic_debugger.runtime.command_runner import CommandRunner
from agentic_debugger.runtime.pdb_session import PdbSession
from agentic_debugger.runtime.workspace import TaskWorkspace

#: Kept open on purpose by the cleanup-failure scenario (Windows semantics).
_KEEP_OPEN: list[Any] = []

_MAX_STEPS = 100000
_MAX_HOLD_SECONDS = 3600
_MAX_TASK_ID_CHARS = 128
_MAX_MODULE_CHARS = 256
_MAX_FOCUS_CHARS = 128
_MAX_PATH_CHARS = 2048


class ScenarioInputError(ApplicationInputError):
    """Raised when a scenario receives malformed parameters."""


@dataclass
class ScenarioContext:
    """Bounded execution context handed to one scenario."""

    work_dir: Path
    token: CancellationToken
    journal: Optional[SessionEventJournal] = None
    emitter: Optional[SessionEventEmitter] = None
    run_id: Optional[str] = None
    #: Durable session directory (journal parent).  Production sources
    #: persist app-owned artifacts here so they survive the disposable work
    #: directory cleanup (Task 7).
    session_dir: Optional[Path] = None


def _require_int(params: Mapping[str, Any], key: str, minimum: int, maximum: int) -> int:
    value = params.get(key)
    if type(value) is not int or isinstance(value, bool):
        raise ScenarioInputError(f"scenario param {key!r} must be an integer")
    if not minimum <= value <= maximum:
        raise ScenarioInputError(
            f"scenario param {key!r} must be within [{minimum}, {maximum}]"
        )
    return value


def _require_number(
    params: Mapping[str, Any], key: str, minimum: float, maximum: float
) -> float:
    value = params.get(key)
    if type(value) is not int and type(value) is not float:
        raise ScenarioInputError(f"scenario param {key!r} must be a number")
    if isinstance(value, bool):
        raise ScenarioInputError(f"scenario param {key!r} must be a number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ScenarioInputError(f"scenario param {key!r} must be finite")
    if not minimum <= number <= maximum:
        raise ScenarioInputError(
            f"scenario param {key!r} must be within [{minimum}, {maximum}]"
        )
    return number


def _require_path(params: Mapping[str, Any], key: str) -> Path:
    value = params.get(key)
    if type(value) is not str or not value:
        raise ScenarioInputError(f"scenario param {key!r} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ScenarioInputError(f"scenario param {key!r} must be UTF-8 text")
    if len(encoded) > _MAX_PATH_CHARS:
        raise ScenarioInputError(f"scenario param {key!r} exceeds the path bound")
    return Path(value)


def _require_text(params: Mapping[str, Any], key: str, maximum: int) -> str:
    value = params.get(key)
    if type(value) is not str or not value:
        raise ScenarioInputError(f"scenario param {key!r} must be a non-empty string")
    if len(value.encode("utf-8")) > maximum:
        raise ScenarioInputError(f"scenario param {key!r} exceeds the byte bound")
    return value


def _unknown_params(params: Mapping[str, Any], known: set[str]) -> None:
    extra = set(params.keys()) - known
    if extra:
        raise ScenarioInputError(f"unknown scenario params: {sorted(extra)}")


def _curated_fixture_dir(task_id: str) -> Path:
    import agentic_debugger

    package_dir = Path(agentic_debugger.__file__).resolve().parent
    fixture_dir = package_dir / "datasets" / "curated" / task_id
    if not (fixture_dir / "task.json").is_file():
        raise ScenarioInputError(f"curated task manifest is missing: {task_id}")
    return fixture_dir


def _scenario_synthetic_work(ctx: ScenarioContext, params: Mapping[str, Any]) -> None:
    _unknown_params(params, {"steps", "step_interval_seconds"})
    steps = _require_int(params, "steps", 1, _MAX_STEPS)
    interval = _require_number(params, "step_interval_seconds", 0.0, 60.0)
    for _ in range(steps):
        ctx.token.check()
        if interval > 0:
            time.sleep(interval)
    ctx.token.check()


def _scenario_sleep_child(ctx: ScenarioContext, params: Mapping[str, Any]) -> None:
    _unknown_params(params, {"duration_seconds", "timeout_seconds", "pid_file", "result_file"})
    duration = _require_int(params, "duration_seconds", 1, _MAX_HOLD_SECONDS)
    timeout = _require_int(params, "timeout_seconds", 1, _MAX_HOLD_SECONDS)
    pid_file = _require_path(params, "pid_file")
    result_file = params.get("result_file")
    if result_file is not None:
        result_file = _require_path(params, "result_file")
    source_dir = ctx.work_dir / "cmd_src"
    source_dir.mkdir(parents=False, exist_ok=False)
    (source_dir / "placeholder.txt").write_text("x", encoding="utf-8")
    workspace = TaskWorkspace(str(source_dir), parent_dir=str(ctx.work_dir))
    runner = CommandRunner(workspace)
    code = (
        "import os, sys, time; "
        "open(sys.argv[1], 'w', encoding='utf-8').write(str(os.getpid())); "
        "time.sleep(int(sys.argv[2]))"
    )
    result = runner.run(
        [sys.executable, "-c", code, str(pid_file), str(duration)],
        ".",
        float(timeout),
        cancel_check=ctx.token.check,
    )
    if result_file is not None:
        import json

        result_file.write_text(
            json.dumps(result.to_mapping(), ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )


def _write_pdb_driver(module_path: Path, module_name: str, focus: str) -> int:
    original = module_path.read_text(encoding="utf-8")
    call_text = f"{module_name}.{focus}(list(range(10)), 3)"
    driver = "\n" + f"import {module_name}\n" + call_text + "\n"
    module_path.write_text(original + driver, encoding="utf-8", newline="\n")
    lines = (original + driver).splitlines()
    return lines.index(call_text) + 1


def _start_pdb_session(
    ctx: ScenarioContext, params: Mapping[str, Any], *, wait_for_cancel: bool
) -> dict[str, Any]:
    _unknown_params(params, {"task_id", "module", "focus", "diag_path", "hold_seconds"})
    task_id = _require_text(params, "task_id", _MAX_TASK_ID_CHARS)
    module = _require_text(params, "module", _MAX_MODULE_CHARS)
    focus = _require_text(params, "focus", _MAX_FOCUS_CHARS)
    diag_path = _require_path(params, "diag_path")
    diag: dict[str, Any] = {
        "pdb_worker_pid": None,
        "pdb_worker_gone_after_stop": None,
    }
    fixture_dir = _curated_fixture_dir(task_id)
    workspace = TaskWorkspace(str(fixture_dir), parent_dir=str(ctx.work_dir))
    module_path = Path(workspace.root) / module
    if not module_path.is_file():
        raise ScenarioInputError(f"PDB module is missing from the fixture: {module}")
    breakpoint_line = _write_pdb_driver(module_path, module[:-3], focus)
    session = PdbSession(workspace)
    session.start()
    proc = getattr(session, "_proc", None)
    diag["pdb_worker_pid"] = proc.pid if proc is not None else None
    # The pid is written early so tests can observe the live PDB worker
    # before any cancellation.
    _write_diag(diag_path, diag)
    try:
        session.start_paused_target(module, [breakpoint_line])
        session.get_stack_summary()
        if wait_for_cancel:
            while True:
                ctx.token.check()
                time.sleep(0.05)
        else:
            ctx.token.check()
    except CancellationError:
        session.stop()
        diag["pdb_worker_gone_after_stop"] = not session.is_alive
        _write_diag(diag_path, diag)
        raise
    finally:
        if session.is_alive:
            session.stop()
    diag["pdb_worker_gone_after_stop"] = not session.is_alive
    _write_diag(diag_path, diag)
    return diag


def _write_diag(diag_path: Path, diag: Mapping[str, Any]) -> None:
    import json

    diag_path.write_text(
        json.dumps(dict(diag), ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def _scenario_pdb_session(ctx: ScenarioContext, params: Mapping[str, Any]) -> None:
    _start_pdb_session(ctx, params, wait_for_cancel=False)


def _scenario_pdb_cooperative_cancel(
    ctx: ScenarioContext, params: Mapping[str, Any]
) -> None:
    _start_pdb_session(ctx, params, wait_for_cancel=True)


def _scenario_pdb_ignore_cancel(ctx: ScenarioContext, params: Mapping[str, Any]) -> None:
    _unknown_params(params, {"task_id", "module", "focus", "diag_path", "hold_seconds"})
    hold = _require_int(params, "hold_seconds", 1, _MAX_HOLD_SECONDS)
    _start_pdb_session(ctx, params, wait_for_cancel=False)
    time.sleep(hold)  # deliberately ignores cooperative cancellation


def _scenario_ignore_cancel(ctx: ScenarioContext, params: Mapping[str, Any]) -> None:
    _unknown_params(params, {"hold_seconds"})
    hold = _require_int(params, "hold_seconds", 1, _MAX_HOLD_SECONDS)
    time.sleep(hold)  # deliberately ignores cooperative cancellation


def _scenario_ignore_cancel_with_child(
    ctx: ScenarioContext, params: Mapping[str, Any]
) -> None:
    _unknown_params(params, {"hold_seconds", "child_duration_seconds", "pid_file"})
    hold = _require_int(params, "hold_seconds", 1, _MAX_HOLD_SECONDS)
    child_duration = _require_int(params, "child_duration_seconds", 1, _MAX_HOLD_SECONDS)
    pid_file = _require_path(params, "pid_file")
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(int(__import__('sys').argv[1]))",
            str(child_duration),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid_file.write_text(str(child.pid), encoding="utf-8")
    time.sleep(hold)  # deliberately ignores cooperative cancellation


def _scenario_crash_hard(ctx: ScenarioContext, params: Mapping[str, Any]) -> None:
    _unknown_params(params, set())
    os._exit(7)  # noqa: PLR1722 - intentional hard crash, no Python cleanup


def _scenario_crash(ctx: ScenarioContext, params: Mapping[str, Any]) -> None:
    _unknown_params(params, set())
    raise RuntimeError("scenario crash")


def _scenario_cleanup_failure(ctx: ScenarioContext, params: Mapping[str, Any]) -> None:
    _unknown_params(params, set())
    locked = ctx.work_dir / "locked"
    locked.mkdir(parents=False, exist_ok=False)
    handle = (locked / "keep.txt").open("w", encoding="utf-8")
    handle.write("x")
    handle.flush()
    _KEEP_OPEN.append(handle)
    if os.name != "nt":
        os.chmod(locked, 0o500)


def _scenario_break_journal(ctx: ScenarioContext, params: Mapping[str, Any]) -> None:
    _unknown_params(params, set())
    if ctx.journal is None:
        raise ScenarioInputError("break_journal requires the journal context")
    ctx.journal.close()  # the next journal append fails out of band


def _scenario_emit_large_event(ctx: ScenarioContext, params: Mapping[str, Any]) -> None:
    """Append one valid Task-1 event well above 64 KiB to the journal.

    Proves that valid large ``SessionEvent`` records (e.g.
    ``debugger.locals_observed`` with the full 512-local bound) survive the
    durable journal and the parent's journal catch-up.  The event is emitted
    through the session's shared emission authority (the coordinator's
    emitter), so the sequence stays the journal's single authority's.
    """
    _unknown_params(params, set())
    if ctx.emitter is None:
        raise ScenarioInputError("emit_large_event requires the emitter context")
    from agentic_debugger.application.events import SessionEventKind

    locals_records = [
        {"name": f"var_{index:03d}", "summary": "value-" + "x" * 280}
        for index in range(512)
    ]
    ctx.emitter.emit(
        SessionEventKind.DEBUGGER_LOCALS_OBSERVED,
        {"pause_generation": 0, "locals": locals_records},
    )


def _scenario_emit_enriched_stream(ctx: ScenarioContext, params: Mapping[str, Any]) -> None:
    """Append a bounded representative Task-4 enriched event prefix.

    Proves that every new Task-4 event kind (controller.transition,
    debugger started/location/stack/locals, source snapshots, patch
    lifecycle incl. apply-failed, diagnosis, verifier stages/completed)
    survives the durable journal and parent catch-up as one coherent
    enriched stream.  This is internal non-product scenario evidence only;
    the real deterministic source wiring is a later roadmap task.
    """
    _unknown_params(params, set())
    if ctx.emitter is None:
        raise ScenarioInputError("emit_enriched_stream requires the emitter context")
    from agentic_debugger.application.events import (
        SessionEventKind,
        SourceSnapshotStage,
    )

    emit = ctx.emitter.emit

    emit(
        SessionEventKind.CONTROLLER_TRANSITION,
        {"source_state": "Reproduce", "target_state": "Understand", "reason": "reproduced"},
    )
    emit(
        SessionEventKind.DEBUGGER_STARTED,
        {"script": "profile.py", "breakpoints": ["profile.py:12"]},
    )
    emit(
        SessionEventKind.DEBUGGER_LOCATION_CHANGED,
        {"script": "profile.py", "line": 12, "function": "format_display_name", "pause_generation": 1},
    )
    emit(
        SessionEventKind.DEBUGGER_STACK_OBSERVED,
        {
            "pause_generation": 1,
            "frames": [
                {"index": 0, "function": "format_display_name", "file": "profile.py", "line": 12, "is_current": True}
            ],
        },
    )
    emit(
        SessionEventKind.DEBUGGER_LOCALS_OBSERVED,
        {
            "pause_generation": 1,
            "locals": [
                {"name": "display_name", "summary": "None"},
                {"name": "other", "summary": "<str len=4>"},
            ],
        },
    )
    emit(
        SessionEventKind.SOURCE_SNAPSHOT,
        {
            "path": "profile.py",
            "sha256": "a" * 64,
            "text": "def format_display_name(name, fallback):\n    return name or fallback\n",
            "line_count": 2,
            "truncated": False,
            "stage": SourceSnapshotStage.INITIAL.value,
        },
    )
    emit(
        SessionEventKind.DIAGNOSIS_RECORDED,
        {"text": "fallback is ignored", "file_path": "profile.py", "symbol": "format_display_name", "confidence": "medium"},
    )
    emit(
        SessionEventKind.PATCH_PROPOSED,
        {
            "attempt_index": 0,
            "patch_sha256": "b" * 64,
            "patch_text": "--- a/profile.py\n+++ b/profile.py\n@@ -1,2 +1,2 @@\n",
        },
    )
    emit(
        SessionEventKind.PATCH_APPLIED,
        {"attempt_index": 0, "changed_files": ["profile.py"], "syntax_passed": True},
    )
    emit(
        SessionEventKind.SOURCE_SNAPSHOT,
        {
            "path": "profile.py",
            "sha256": "c" * 64,
            "text": "def format_display_name(name, fallback):\n    return name if name else fallback\n",
            "line_count": 2,
            "truncated": False,
            "stage": SourceSnapshotStage.APPLIED.value,
        },
    )
    emit(
        SessionEventKind.PATCH_REVERTED,
        {"attempt_index": 0},
    )
    emit(
        SessionEventKind.PATCH_PROPOSED,
        {
            "attempt_index": 1,
            "patch_sha256": "d" * 64,
            "patch_text": "--- a/profile.py\n+++ b/profile.py\n@@ -1 +1 @@\n",
        },
    )
    emit(
        SessionEventKind.PATCH_APPLY_FAILED,
        {"attempt_index": 1, "apply_failure_reason": "hunk does not apply"},
    )
    emit(SessionEventKind.VERIFIER_STARTED, {})
    emit(
        SessionEventKind.VERIFIER_STAGE_STARTED,
        {"stage": "prepare_workspace"},
    )
    emit(
        SessionEventKind.VERIFIER_STAGE_COMPLETED,
        {"stage": "prepare_workspace", "status": "completed"},
    )
    emit(
        SessionEventKind.VERIFIER_STAGE_STARTED,
        {"stage": "baseline_reproduction"},
    )
    emit(
        SessionEventKind.VERIFIER_STAGE_COMPLETED,
        {"stage": "baseline_reproduction", "status": "completed"},
    )
    emit(
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
    )


SCENARIOS: Mapping[str, Callable[[ScenarioContext, Mapping[str, Any]], None]] = {
    "synthetic_work": _scenario_synthetic_work,
    "sleep_child": _scenario_sleep_child,
    "pdb_session": _scenario_pdb_session,
    "pdb_cooperative_cancel": _scenario_pdb_cooperative_cancel,
    "pdb_ignore_cancel": _scenario_pdb_ignore_cancel,
    "ignore_cancel": _scenario_ignore_cancel,
    "ignore_cancel_with_child": _scenario_ignore_cancel_with_child,
    "crash_hard": _scenario_crash_hard,
    "crash": _scenario_crash,
    "cleanup_failure": _scenario_cleanup_failure,
    "break_journal": _scenario_break_journal,
    "emit_large_event": _scenario_emit_large_event,
    "emit_enriched_stream": _scenario_emit_enriched_stream,
}

#: Publicly documented scenario names (worker dispatch fails closed on others).
SCENARIO_NAMES = frozenset(SCENARIOS.keys())


def run_scenario(
    name: str,
    ctx: ScenarioContext,
    params: Mapping[str, Any],
) -> None:
    """Dispatch one bounded internal scenario; unknown names fail closed."""
    scenario = SCENARIOS.get(name)
    if scenario is None:
        raise ScenarioInputError(f"unknown scenario: {name!r}")
    scenario(ctx, params)


__all__ = [
    "SCENARIOS",
    "SCENARIO_NAMES",
    "ScenarioContext",
    "ScenarioInputError",
    "run_scenario",
]
