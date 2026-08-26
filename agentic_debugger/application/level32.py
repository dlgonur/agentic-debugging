"""Application bridge for the authoritative Level-32 Cookiecutter operator.

This module owns no benchmark logic.  It selects canonical Ollama profiles,
allocates an unused treatment revision through the existing operator helper,
and supervises exactly one invocation of that operator while projecting only
bounded, observed evidence into the normal application journal.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Tuple

from agentic_debugger.application.emitter import SessionEventEmitter
from agentic_debugger.application.command_transport import _terminate_command_tree
from agentic_debugger.application.events import (
    MAX_PATCH_TEXT_CHARS,
    OperatorStage,
    SessionEvent,
    SessionEventKind,
    SessionPhase,
    SessionStatus,
    SessionTerminationReason,
    SourceKind,
    VerifierStage,
    contains_credential_shape,
)
from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.journal import SessionEventJournal
from agentic_debugger.application.session import (
    SessionBudgets,
    SessionId,
    SessionResult,
    SessionSpec,
)
from agentic_debugger.application.worker_protocol import WorkerLiveness
from agentic_debugger.application.sources import ExecutionSourceSpec

LEVEL32_TASK_ID = "audreyr__cookiecutter-967"
LEVEL32_OPERATOR_SCRIPT = "scripts/run_cookiecutter_967_pdb_proof.py"

@dataclass(frozen=True)
class LadderTaskMetadata:
    """One canonical product label set for an accepted ladder rung."""

    title: str
    task_id: str
    debugger: str
    treatment: str
    evaluation: str


LADDER_TASKS: tuple[LadderTaskMetadata, ...] = (
    LadderTaskMetadata(
        "Level 6/100", "pdb-required-boundary-006", "Exact PDB required",
        "Accepted Level-6 contract", "Independent verifier",
    ),
    LadderTaskMetadata(
        "Level 12/100", "pdb-required-caller-callee-007", "Exact PDB required",
        "Accepted Level-12 contract", "Independent verifier",
    ),
    LadderTaskMetadata(
        "Level 18/100", "pdb-required-multistage-units-008", "Exact PDB required",
        "Accepted Level-18 contract", "Independent verifier",
    ),
    LadderTaskMetadata(
        "Level 32/100 — Cookiecutter #967", LEVEL32_TASK_ID, "Exact PDB required",
        "Frozen Level-32", "Official SWE-rebench",
    ),
)
LADDER_TASK_IDS = frozenset(item.task_id for item in LADDER_TASKS)


def ladder_task_options() -> tuple[tuple[str, str], ...]:
    """The four accepted product rungs, retaining their canonical IDs."""

    return tuple((f"{item.title} · {item.task_id}", item.task_id) for item in LADDER_TASKS)


def ladder_task_metadata(task_id: str) -> LadderTaskMetadata:
    """Return the immutable metadata for one accepted rung."""

    for item in LADDER_TASKS:
        if item.task_id == task_id:
            return item
    raise KeyError(task_id)


def is_ladder_task(task_id: Optional[str]) -> bool:
    return task_id in LADDER_TASK_IDS


@dataclass(frozen=True)
class Level32ModelProfile:
    """Safe UI projection of one canonical Ollama Cloud profile."""

    alias: str
    display_name: str
    readiness: str
    transport_config_fingerprint: str

    @property
    def profile_id(self) -> str:
        return self.alias


def level32_model_profiles() -> Tuple[Level32ModelProfile, ...]:
    """Return only canonical, live-verified Level-32-eligible profiles.

    Importing this registry is local and read-only.  In particular, this
    function never calls Ollama or sends an inference request; the operator's
    own preflight remains the final availability gate at Start time.
    """

    from scripts.ollama_cloud_command_adapter import (
        CLOUD_MODELS,
        is_treatment_eligible,
        transport_config_fingerprint,
    )

    return tuple(
        Level32ModelProfile(
            alias=spec.local_alias,
            display_name=spec.upstream_model,
            readiness=spec.readiness,
            transport_config_fingerprint=transport_config_fingerprint(spec),
        )
        for spec in sorted(CLOUD_MODELS.values(), key=lambda item: item.local_alias)
        if is_treatment_eligible(spec)
    )


ollama_cloud_model_profiles = level32_model_profiles


def next_level32_treatment(repository_root: str | Path, model: str) -> tuple[int, str, Path]:
    """Allocate the next unused revision using the operator's identity rules."""

    import scripts.run_cookiecutter_967_pdb_proof as operator

    revision = operator.next_unused_treatment_revision(repository_root, model)
    treatment_id = operator._treatment_id_for_model(model, revision)
    output_dir = (
        Path(repository_root).resolve()
        / operator._default_output_dir_for_model(model, revision)
    ).resolve()
    if output_dir.exists():
        raise RuntimeError(f"Level-32 treatment output already exists: {output_dir}")
    return revision, treatment_id, output_dir


class _OperatorProcess(Protocol):
    pid: int
    returncode: Optional[int]

    def communicate(self) -> tuple[str, str]: ...
    def poll(self) -> Optional[int]: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


ProcessFactory = Callable[..., _OperatorProcess]


def _default_process_factory(*args: Any, **kwargs: Any) -> _OperatorProcess:
    return subprocess.Popen(*args, **kwargs)  # type: ignore[return-value]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_text(value: Any, maximum: int = 4000) -> str:
    text = str(value or "").replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    if len(text) > maximum:
        text = text[: maximum - 3] + "..."
    return "[redacted sensitive subprocess output]" if contains_credential_shape(text) else text


def _write_text(path: Path, value: Any, *, maximum: int = 8192) -> None:
    path.write_text(_safe_text(value, maximum), encoding="utf-8", newline="\n")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _official_verifier_counts(
    official: Mapping[str, Any],
) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    """Project only validated, redacted aggregate counts from official output."""

    if official.get("official_test_execution_proven") is not True:
        return None, None, None, None

    values: dict[str, int] = {}
    for name in (
        "fail_to_pass_total",
        "fail_to_pass_passed",
        "pass_to_pass_total",
        "pass_to_pass_failed",
    ):
        value = official.get(name)
        if type(value) is not int or isinstance(value, bool) or value < 0:
            return None, None, None, None
        values[name] = value
    if values["fail_to_pass_passed"] > values["fail_to_pass_total"]:
        return None, None, None, None
    if values["pass_to_pass_failed"] > values["pass_to_pass_total"]:
        return None, None, None, None
    return (
        values["fail_to_pass_passed"],
        values["fail_to_pass_total"],
        values["pass_to_pass_total"] - values["pass_to_pass_failed"],
        values["pass_to_pass_total"],
    )


class Level32OperatorWorker:
    """LiveWorker-compatible supervisor for one authoritative operator run."""

    def __init__(
        self,
        *,
        session_dir: str | Path,
        session_id: str,
        run_id: str,
        repository_root: str | Path,
        model: Level32ModelProfile,
        revision: int,
        treatment_id: str,
        output_dir: str | Path,
        spec: SessionSpec,
        process_factory: ProcessFactory = _default_process_factory,
    ) -> None:
        self._session_dir = Path(session_dir).resolve()
        self._session_dir.mkdir(parents=True, exist_ok=False)
        self._session_id = session_id
        self._run_id = run_id
        self._repository_root = Path(repository_root).resolve()
        self._model = model
        self._revision = revision
        self._treatment_id = treatment_id
        self._output_dir = Path(output_dir).resolve()
        self._spec = spec
        self._process_factory = process_factory
        self._journal: Optional[SessionEventJournal] = None
        self._emitter: Optional[SessionEventEmitter] = None
        self._process: Optional[_OperatorProcess] = None
        self._events: list[SessionEvent] = []
        self._result: Optional[SessionResult] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._cancel_requested = False
        self._closed = False
        self._progress_path = self._session_dir / "operator.progress.jsonl"
        self._progress_offset = 0
        self._last_progress: tuple[OperatorStage, Optional[str]] | None = None
        self._result_parse_error: Optional[str] = None
        self._stdout_capture_path = self._session_dir / "operator.stdout.capture.txt"
        self._stderr_capture_path = self._session_dir / "operator.stderr.capture.txt"
        self._stdout_stream: Any = None
        self._stderr_stream: Any = None
        self._liveness: Optional[WorkerLiveness] = None
        self._streamed_request_indexes: set[int] = set()
        # Structured-operation state: which debugger facts were already
        # projected live so post-run finalization never duplicates them.
        self._streamed_debugger_started = False
        self._streamed_pdb_observation: Optional[tuple[str, int]] = None
        self._pause_generation = 0
        # Zero-based attempt index of the last streamed applied candidate;
        # the authoritative patch body is enriched onto it at finalization.
        self._last_applied_attempt_index: Optional[int] = None
        #: Stable identity of patch bodies already emitted live
        #: (attempt_index -> sha256).  Finalization re-checks this map so
        #: live milestone + post-run fallback == one durable fact.
        self._patch_bodies_emitted: dict[int, str] = {}

    def _emit_progress(self, stage: OperatorStage, detail: Optional[str] = None) -> None:
        # Distinct (stage, detail) pairs are retained; identical consecutive
        # bare stage records are not meaningful progress.  Details are only
        # ever facts the operator recorded, never synthesized here.
        current = (stage, detail)
        if current == self._last_progress:
            return
        self._last_progress = current
        payload: dict[str, Any] = {"stage": stage.value}
        if detail:
            payload["detail"] = detail
        self._emit(SessionEventKind.OPERATOR_PROGRESS, payload)

    def _drain_progress(self) -> None:
        """Consume only the optional observer channel, never operator logs."""

        try:
            with self._progress_path.open("r", encoding="utf-8") as stream:
                stream.seek(self._progress_offset)
                while True:
                    line = stream.readline()
                    if not line:
                        break
                    self._progress_offset = stream.tell()
                    try:
                        record = json.loads(line)
                        schema = record.get("schema_version")
                        if schema == "operator-progress-v1":
                            stage = OperatorStage(record.get("stage"))
                            detail = record.get("detail")
                            self._emit_progress(stage, detail if isinstance(detail, str) else None)
                        elif schema == "operator-progress-v2":
                            self._consume_progress_v2(record)
                        else:
                            continue
                    except (TypeError, ValueError, json.JSONDecodeError):
                        # A malformed observer record is ignored. The
                        # authoritative operator result remains the source of truth.
                        continue
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            return

    #: Handler-level apply outcomes that are real candidate attempts.
    _TOOL_STATUSES = frozenset({"ok", "error", "rejected", "timeout"})
    _CANDIDATE_PHASES = frozenset({"applied", "rejected", "failed", "reverted"})

    @staticmethod
    def _bounded_operation_text(value: Any, maximum: int = 200) -> Optional[str]:
        if type(value) is not str or not value:
            return None
        if len(value.encode("utf-8")) > maximum or contains_credential_shape(value):
            return None
        return value

    @staticmethod
    def _relative_operation_path(value: Any) -> Optional[str]:
        text = Level32OperatorWorker._bounded_operation_text(value, 256)
        if text is None:
            return None
        if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
            return None
        if text.startswith(("/", "\\")):
            return None
        parts = [part for part in text.replace("\\", "/").split("/") if part]
        if not parts or ".." in parts:
            return None
        return text

    @staticmethod
    def _operation_line(value: Any) -> Optional[int]:
        if type(value) is not int or isinstance(value, bool) or value < 1:
            return None
        return value

    def _consume_operation_record(self, record: Mapping[str, Any]) -> None:
        """Map one strictly validated structured operation to typed events.

        Malformed records drop silently: the authoritative operator result
        remains the source of truth and no fact is ever reconstructed from
        an invalid observation channel record.
        """
        operation = record.get("operation")
        if operation == "candidate_patch_available":
            self._consume_candidate_patch_milestone(record)
            return
        if operation == "tool":
            phase = record.get("phase")
            tool = self._bounded_operation_text(record.get("tool"), 64)
            if tool is None or phase not in ("started", "completed"):
                return
            if phase == "started":
                if set(record) != {"schema_version", "kind", "operation", "phase", "tool"}:
                    return
                self._emit(SessionEventKind.TOOL_STARTED, {"tool_name": tool})
                return
            if set(record) != {
                "schema_version", "kind", "operation", "phase", "tool", "status",
            }:
                return
            status = record.get("status")
            if status not in self._TOOL_STATUSES:
                return
            self._emit(SessionEventKind.TOOL_COMPLETED, {"tool_name": tool, "status": status})
            return
        if operation == "source_inspection":
            if set(record) != {
                "schema_version", "kind", "operation", "tool", "file",
                "start_line", "end_line",
            }:
                return
            tool = self._bounded_operation_text(record.get("tool"), 64)
            file_name = self._relative_operation_path(record.get("file"))
            start = self._operation_line(record.get("start_line"))
            end = self._operation_line(record.get("end_line"))
            if tool is None or file_name is None or start is None or end is None:
                return
            if start > end:
                return
            target = f"{file_name}:{start}-{end}"
            self._emit(
                SessionEventKind.TOOL_COMPLETED,
                {"tool_name": tool, "status": "ok", "target": target},
            )
            return
        if operation == "debugger_active":
            if set(record) != {"schema_version", "kind", "operation", "script", "breakpoint_line"}:
                return
            script = self._relative_operation_path(record.get("script"))
            line = self._operation_line(record.get("breakpoint_line"))
            if script is None or line is None:
                return
            if not self._streamed_debugger_started:
                self._streamed_debugger_started = True
                self._emit(
                    SessionEventKind.DEBUGGER_STARTED,
                    {"script": script, "breakpoints": (f"{script}:{line}",)},
                )
            if self._pause_generation == 0:
                self._pause_generation = 1
                self._emit(
                    SessionEventKind.DEBUGGER_LOCATION_CHANGED,
                    {"script": script, "line": line, "function": None, "pause_generation": 1},
                )
            return
        if operation == "pdb_observation":
            allowed = {"schema_version", "kind", "operation"}
            if not allowed.issubset(set(record)) or set(record) - allowed - {"script", "line"}:
                return
            script = self._relative_operation_path(record.get("script"))
            line = self._operation_line(record.get("line"))
            if ("script" in record or "line" in record) and (script is None or line is None):
                return
            self._pause_generation += 1
            if script is not None and line is not None:
                if self._streamed_pdb_observation is None:
                    self._streamed_pdb_observation = (script, line)
                self._emit(
                    SessionEventKind.DEBUGGER_LOCATION_CHANGED,
                    {"script": script, "line": line, "function": None, "pause_generation": self._pause_generation},
                )
            self._emit(
                SessionEventKind.DEBUGGER_STACK_OBSERVED,
                {"pause_generation": self._pause_generation, "frames": ()},
            )
            return
        if operation == "candidate":
            phase = record.get("phase")
            if phase not in self._CANDIDATE_PHASES:
                return
            attempt = record.get("attempt")
            if type(attempt) is not int or isinstance(attempt, bool) or attempt < 1:
                return
            reason = self._bounded_operation_text(record.get("reason"))
            index = attempt - 1
            if phase == "applied":
                changed = record.get("changed_files")
                if type(changed) is not list or len(changed) > 16:
                    return
                files: list[str] = []
                for item in changed:
                    path = self._relative_operation_path(item)
                    if path is None:
                        return
                    files.append(path)
                base = {"attempt_index": index, "changed_files": tuple(files), "syntax_passed": None}
                self._last_applied_attempt_index = index
                self._emit(SessionEventKind.PATCH_APPLIED, base)
            elif phase == "rejected":
                self._emit(
                    SessionEventKind.PATCH_REJECTED,
                    {"attempt_index": index, "rejection_reason": reason or "candidate rejected by patch validation"},
                )
            elif phase == "failed":
                self._emit(
                    SessionEventKind.PATCH_APPLY_FAILED,
                    {"attempt_index": index, "apply_failure_reason": reason or "candidate patch apply failed"},
                )
            else:
                self._emit(SessionEventKind.PATCH_REVERTED, {"attempt_index": index})
            return
        if operation == "controller_step":
            if set(record) != {"schema_version", "kind", "operation", "step_index", "directive_kind"}:
                return
            step_index = record.get("step_index")
            if type(step_index) is not int or isinstance(step_index, bool) or step_index < 0:
                return
            directive_kind = self._bounded_operation_text(record.get("directive_kind"), 64)
            self._emit(
                SessionEventKind.CONTROLLER_STEP,
                {"step_index": step_index, "directive_kind": directive_kind, "stop_reason": None},
            )
            return
        # Unknown operation kinds are ignored (fail closed).

    def _consume_progress_v2(self, record: Mapping[str, Any]) -> None:
        """Accept additive safe v2 observer records; malformed records drop."""
        kind = record.get("kind")
        if kind == "operation":
            self._consume_operation_record(record)
            return
        if kind == "liveness":
            allowed = {
                "schema_version", "kind", "request_index", "request_elapsed_seconds",
                "last_activity_age_seconds", "transport_alive", "watchdog_idle_seconds",
            }
            if set(record) != allowed:
                return
            request_index = record.get("request_index")
            if request_index is not None and (type(request_index) is not int or request_index < 0):
                return
            values = [record.get("request_elapsed_seconds"), record.get("last_activity_age_seconds"), record.get("watchdog_idle_seconds")]
            if any(type(value) not in (int, float) or isinstance(value, bool) or value < 0 for value in values):
                return
            if type(record.get("transport_alive")) is not bool:
                return
            with self._lock:
                self._liveness = WorkerLiveness(
                    request_index=request_index,
                    request_elapsed_seconds=float(values[0]),
                    last_activity_age_seconds=float(values[1]),
                    transport_alive=record["transport_alive"],
                    watchdog_idle_seconds=float(values[2]),
                )
            return
        if kind == "model_request":
            detail = record.get("detail")
            if type(detail) is not str or not detail.startswith("request "):
                return
            number = detail.split(" ", 2)[1]
            if not number.isdigit() or int(number) < 1:
                return
            index = int(number) - 1
            if index not in self._streamed_request_indexes:
                self._streamed_request_indexes.add(index)
                self._emit(SessionEventKind.MODEL_REQUEST_STARTED, {"request_index": index})
            self._emit_progress(OperatorStage.MODEL_RUNNING, detail)
            return
        if kind == "model_request_completed":
            detail = record.get("detail")
            if type(detail) is not str or not detail.startswith("request "):
                return
            number = detail.split(" ", 2)[1]
            if not number.isdigit() or int(number) < 1:
                return
            index = int(number) - 1
            if index in self._streamed_request_indexes:
                self._emit(SessionEventKind.MODEL_REQUEST_COMPLETED, {"request_index": index, "status": "ok"})
                self._streamed_request_indexes.remove(index)
            return
        if kind == "official_execution_proven":
            # The typed milestone is durable operator evidence: real official
            # test execution was observed.  Stage/detail remain unchanged for
            # v1 history readability.
            allowed = {
                "schema_version", "kind", "stage", "detail",
                "official_execution_proven",
            }
            if set(record) != allowed or record.get("official_execution_proven") is not True:
                return
            try:
                stage = OperatorStage(record["stage"])
            except (KeyError, ValueError):
                return
            detail = record.get("detail")
            if type(detail) is not str or not detail or contains_credential_shape(detail):
                return
            current = (stage, detail)
            if current == self._last_progress:
                # Same fact already emitted: enrich nothing, re-mark typed.
                return
            self._last_progress = current
            self._emit(
                SessionEventKind.OPERATOR_PROGRESS,
                {"stage": stage.value, "detail": detail, "official_execution_proven": True},
            )
            return
        # Durable v2 operational records have a safe stage and optional
        # bounded label only. They intentionally remain one SessionEvent-v1
        # ``operator.progress`` fact, so v1 history stays readable and final
        # result projection never re-emits a duplicate tool/PDB/verifier fact.
        allowed = {"schema_version", "kind", "stage", "detail"}
        if set(record) != allowed or type(kind) is not str or type(record.get("detail")) not in (str, type(None)):
            return
        try:
            stage = OperatorStage(record["stage"])
        except (KeyError, ValueError):
            return
        detail = record.get("detail")
        if detail is not None and (not detail or len(detail.encode("utf-8")) > 512 or contains_credential_shape(detail)):
            return
        self._emit_progress(stage, detail)

    @property
    def pid(self) -> Optional[int]:
        return self._process.pid if self._process is not None else None

    @property
    def events(self) -> Tuple[SessionEvent, ...]:
        with self._lock:
            return tuple(self._events)

    @property
    def liveness(self) -> Optional[WorkerLiveness]:
        with self._lock:
            return self._liveness

    @property
    def session_dir(self) -> Path:
        return self._session_dir

    def _emit(self, kind: SessionEventKind, payload: Mapping[str, Any], *, phase: Any = None) -> None:
        assert self._emitter is not None
        event = self._emitter.emit(kind, payload, controller_phase=phase)
        with self._lock:
            self._events.append(event)

    def start(self) -> Optional[SessionResult]:
        if self._thread is not None:
            raise RuntimeError("Level-32 operator worker already started")
        self._journal = SessionEventJournal(
            self._session_dir / "session.events.jsonl",
            session_id=self._session_id,
            task_id=self._spec.task_id,
            source_kind=SourceKind.LEVEL32_OPERATOR,
        )
        self._emitter = SessionEventEmitter(
            session_id=self._session_id,
            task_id=self._spec.task_id,
            source_kind=SourceKind.LEVEL32_OPERATOR,
            sink=self._journal,
        )
        self._emit(SessionEventKind.SESSION_CREATED, {"spec_fingerprint": self._spec.fingerprint()})
        self._emitter.bind_run_id(self._run_id)
        self._emit(SessionEventKind.SESSION_STARTED, {})
        self._emit_progress(OperatorStage.STARTING)
        self._emit(
            SessionEventKind.MODEL_CONFIGURED,
            {
                "profile_id": self._model.alias,
                "config_fingerprint": self._model.transport_config_fingerprint,
                "display_name": self._model.display_name,
                "protocol_version": "1.3",
                "tool_version": "level32-frozen-operator",
                "treatment_revision": self._revision,
                "treatment_id": self._treatment_id,
                "result_location": str(self._output_dir.relative_to(self._repository_root)),
            },
        )
        self._emit(
            SessionEventKind.SESSION_STATUS_CHANGED,
            {"status": "running", "phase": SessionPhase.EXECUTING_TOOL.value},
        )
        self._emit_progress(OperatorStage.PREFLIGHT)
        argv = [
            sys.executable,
            str(self._repository_root / LEVEL32_OPERATOR_SCRIPT),
            "--repository-root", str(self._repository_root),
            "--output-dir", str(self._output_dir),
            "--model", self._model.alias,
            "--treatment-revision", str(self._revision),
            "--live",
            "--confirm-live-model-access",
            "--progress-file", str(self._session_dir / "operator.progress.jsonl"),
        ]
        try:
            _write_json(
                self._session_dir / "operator.command.json",
                {
                    "schema_version": "level32-subprocess-command-v1",
                    "executable": _safe_text(sys.executable, 1024),
                    "argv": [_safe_text(item, 2048) for item in argv],
                    "cwd": _safe_text(self._repository_root, 2048),
                    "shell": False,
                },
            )
        except OSError:
            pass
        try:
            self._stdout_stream = self._stdout_capture_path.open(
                "w", encoding="utf-8", newline="\n"
            )
            self._stderr_stream = self._stderr_capture_path.open(
                "w", encoding="utf-8", newline="\n"
            )
            self._process = self._process_factory(
                argv,
                cwd=str(self._repository_root),
                stdin=subprocess.DEVNULL,
                stdout=self._stdout_stream,
                stderr=self._stderr_stream,
                shell=False,
            )
        except Exception as exc:
            self._close_capture_streams()
            self._finish(returncode=2, stdout="", stderr=f"operator launch failed: {exc}", result=None)
            return None
        self._thread = threading.Thread(target=self._monitor, name="level32-operator", daemon=True)
        self._thread.start()
        return None

    def _monitor(self) -> None:
        assert self._process is not None
        try:
            while self._process.poll() is None:
                self._drain_progress()
                threading.Event().wait(0.05)
            self._drain_progress()
            returncode = self._process.returncode
            if isinstance(self._process, subprocess.Popen):
                # Do not call communicate() here.  The operator can launch a
                # Docker child that inherits the parent's output handles; a
                # direct-process exit then leaves communicate() waiting for
                # EOF that belongs to a descendant.  File-backed capture is
                # bounded at projection time and has no pipe-drain wait.
                self._close_capture_streams()
                stdout = self._read_capture(self._stdout_capture_path)
                stderr = self._read_capture(self._stderr_capture_path)
            else:
                # Provider-free fakes retain the small process seam used by
                # unit tests; production Popen never enters this branch.
                stdout, stderr = self._process.communicate()
                self._close_capture_streams()
            if returncode is None:
                returncode = self._process.poll()
        except Exception as exc:
            self._close_capture_streams()
            stdout, stderr, returncode = "", f"operator supervision failed: {exc}", 2
        result = self._read_result()
        self._finish(returncode=returncode if returncode is not None else 2, stdout=stdout, stderr=stderr, result=result)

    @staticmethod
    def _read_capture(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeError):
            return ""

    def _close_capture_streams(self) -> None:
        for attribute in ("_stdout_stream", "_stderr_stream"):
            stream = getattr(self, attribute)
            if stream is None:
                continue
            try:
                stream.close()
            except Exception:
                pass
            setattr(self, attribute, None)

    def _remove_capture_files(self) -> None:
        for path in (self._stdout_capture_path, self._stderr_capture_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # The bounded operator.stdout/stderr evidence remains the
                # durable fallback if a capture file cannot be removed.
                pass

    def _read_result(self) -> Optional[dict[str, Any]]:
        path = self._output_dir / "result.json"
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._result_parse_error = _safe_text(f"{type(exc).__name__}: {exc}", 512)
            return None
        if not isinstance(value, dict):
            self._result_parse_error = "result.json is not a JSON object"
            return None
        return value

    def _cleanup_evidence(self) -> bool:
        value = self._read_live_results()
        reporting = value.get("reporting") if isinstance(value, Mapping) else None
        return bool(
            isinstance(reporting, Mapping)
            and reporting.get("completed") is True
            and reporting.get("cleanup") == "cleaned"
        )

    def _read_live_results(self) -> Optional[dict[str, Any]]:
        path = self._output_dir / "live-results.json"
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _pdb_proof_payload(
        self, result: Optional[Mapping[str, Any]]
    ) -> Optional[dict[str, Any]]:
        sources = tuple(
            source
            for source in (result, self._read_live_results())
            if isinstance(source, Mapping)
        )
        for source in sources:
            explicit = source.get("pdb_proof")
            if isinstance(explicit, Mapping):
                count = explicit.get("successful_observation_count")
                script = explicit.get("script")
                line = explicit.get("breakpoint_line")
                if (
                    explicit.get("observed") is True
                    and type(count) is int
                    and count > 0
                    and isinstance(script, str)
                    and type(line) is int
                    and line > 0
                ):
                    return {"script": script, "line": line, "count": count}

            # The live operator stores the exact proof in its canonical
            # trajectory, not in the summary measurements.  A count alone is
            # insufficient: it must never be converted into a guessed file
            # or breakpoint line.
            events_jsonl = source.get("events_jsonl")
            if not isinstance(events_jsonl, str):
                continue
            measurements = source.get("measurements")
            count = (
                measurements.get("successful_pdb_observation_count")
                if isinstance(measurements, Mapping)
                else None
            )
            if type(count) is not int or count <= 0:
                continue
            for line_text in events_jsonl.splitlines():
                try:
                    event = json.loads(line_text)
                except (TypeError, json.JSONDecodeError):
                    continue
                payload = event.get("payload") if isinstance(event, Mapping) else None
                observation = payload.get("observation") if isinstance(payload, Mapping) else None
                observation_payload = (
                    observation.get("payload")
                    if isinstance(observation, Mapping)
                    else None
                )
                proof = (
                    observation_payload.get("proof")
                    if isinstance(observation_payload, Mapping)
                    else None
                )
                if not isinstance(proof, Mapping) or proof.get("exact_reproduction") is not True:
                    continue
                script = proof.get("production_file") or observation_payload.get("script")
                breakpoint_line = proof.get("breakpoint_line") or observation_payload.get("breakpoint_line")
                if isinstance(script, str) and type(breakpoint_line) is int and breakpoint_line > 0:
                    return {"script": script, "line": breakpoint_line, "count": count}
        return None

    def _record_process_evidence(
        self,
        *,
        returncode: int,
        stdout: str,
        stderr: str,
        result_present: bool,
    ) -> None:
        try:
            _write_text(self._session_dir / "operator.stdout.txt", stdout)
            _write_text(self._session_dir / "operator.stderr.txt", stderr)
            _write_json(
                self._session_dir / "operator.process.json",
                {
                    "schema_version": "level32-subprocess-result-v1",
                    "pid": self.pid,
                    "exit_code": returncode,
                    "result_path": _safe_text(self._output_dir / "result.json", 2048),
                    "result_present": result_present,
                    "result_parse_error": self._result_parse_error,
                },
            )
        except OSError:
            pass

    def _record_artifacts(self) -> None:
        if not self._output_dir.is_dir():
            return
        for path in sorted(self._output_dir.iterdir()):
            if not path.is_file():
                continue
            try:
                relative = path.relative_to(self._repository_root).as_posix()
                digest = _sha256(path)
            except OSError:
                continue
            self._emit(SessionEventKind.ARTIFACT_WRITTEN, {"path": relative, "sha256": digest})

    def _emit_candidate_patch_text(self) -> None:
        """Finalization fallback for the live patch body (historical v1).

        Observer-only and fail-open: for operators that never emitted the
        live ``candidate_patch_available`` milestone, read the authoritative
        ``candidate.patch`` artifact once after process exit and emit the
        typed body.  Idempotent with the live path via
        ``_patch_bodies_emitted``: a body already emitted live is never
        re-emitted.
        """
        if self._last_applied_attempt_index is None:
            return
        self._read_and_emit_candidate_patch(
            attempt_index=self._last_applied_attempt_index
        )

    def _consume_candidate_patch_milestone(self, record: Mapping[str, Any]) -> None:
        """Handle one validated ``candidate_patch_available`` milestone.

        The milestone carries the authoritative one-based attempt identity
        and the exact sha256 of the raw patch the operator just wrote.
        Reading is strict and fail-closed: the artifact must be the expected
        fresh ``candidate.patch`` in the session output directory, must
        satisfy the patch size/credential bounds, and its sha256 must equal
        the milestone hash.  Only then is the typed ``patch.proposed`` body
        emitted -- live, before official evaluation starts.  Anything else
        drops silently (the operator's authoritative result remains the
        source of truth, and finalization keeps its own fail-open path).
        """
        allowed = {
            "schema_version", "kind", "operation", "attempt", "sha256",
            "candidate_patch",
        }
        if set(record) != allowed:
            return
        if record.get("operation") != "candidate_patch_available":
            return
        if record.get("candidate_patch") != "candidate.patch":
            return
        attempt = record.get("attempt")
        if type(attempt) is not int or isinstance(attempt, bool) or attempt < 1:
            return
        milestone_sha256 = record.get("sha256")
        if type(milestone_sha256) is not str or len(milestone_sha256) != 64:
            return
        try:
            if int(milestone_sha256, 16) < 0:  # pragma: no cover - int() gate
                return
        except ValueError:
            return
        self._read_and_emit_candidate_patch(
            attempt_index=attempt - 1, expected_sha256=milestone_sha256
        )

    def _read_and_emit_candidate_patch(
        self, *, attempt_index: int, expected_sha256: Optional[str] = None
    ) -> None:
        """Read the fresh output-dir candidate.patch bytes and emit the body.

        Shared by the live milestone (hash-verified, strict) and the
        finalization fallback (fail-open, for historical v1/no-milestone
        operators).  Provenance is byte-exact: the file is read as raw
        bytes, a conservative byte-size bound is enforced, the SHA-256 of
        the exact bytes must match the milestone hash (no newline
        normalization), the bytes are decoded as strict UTF-8, and the
        existing text/credential validation applies before the typed body
        is emitted.  Idempotent per (attempt_index, sha256): a body already
        emitted live is never re-emitted by finalization.
        """
        if self._patch_bodies_emitted.get(attempt_index) is not None:
            return
        path = self._output_dir / "candidate.patch"
        try:
            raw = path.read_bytes()
        except OSError:
            return
        if len(raw) > MAX_PATCH_TEXT_CHARS:
            return
        digest = hashlib.sha256(raw).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            return
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return
        if not text.strip():
            return
        if contains_credential_shape(text):
            return
        self._patch_bodies_emitted[attempt_index] = digest
        self._emit(
            SessionEventKind.PATCH_PROPOSED,
            {
                "attempt_index": attempt_index,
                "patch_sha256": digest,
                "patch_text": text,
            },
        )

    def _finish(self, *, returncode: int, stdout: str, stderr: str, result: Optional[dict[str, Any]]) -> None:
        if self._result is not None:
            return
        accepted = bool(result and result.get("accepted") is True)
        operator_failure = result.get("operator_failure") if result else None
        cleanup = bool(
            result
            and isinstance(result.get("cleanup"), Mapping)
            and result["cleanup"].get("temporary_source_removed") is True
            and result["cleanup"].get("private_official_material_removed") is True
        )
        cleanup = cleanup or self._cleanup_evidence()
        classification = result.get("classification") if result else None
        self._record_process_evidence(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            result_present=result is not None,
        )
        self._remove_capture_files()
        self._record_artifacts()
        self._emit_candidate_patch_text()
        pdb_proof = self._pdb_proof_payload(result)
        if pdb_proof is not None:
            script = pdb_proof["script"]
            line = pdb_proof["line"]
            # Post-run finalization projects only facts not already streamed
            # live through the structured operation channel: a streamed
            # debugger start or proof observation is never re-emitted.
            if not self._streamed_debugger_started:
                self._streamed_debugger_started = True
                self._emit(
                    SessionEventKind.DEBUGGER_STARTED,
                    {"script": script, "breakpoints": (f"{script}:{line}",)},
                )
            if self._streamed_pdb_observation is None:
                # The extracted proof has already established an exact
                # observed breakpoint. Represent that fact through the
                # existing v1 debugger observation vocabulary; debugger
                # start alone is never treated as proof by the projection.
                self._streamed_pdb_observation = (script, line)
                self._pause_generation = max(1, self._pause_generation + 1)
                self._emit(
                    SessionEventKind.DEBUGGER_LOCATION_CHANGED,
                    {"script": script, "line": line, "function": None, "pause_generation": self._pause_generation},
                )
                self._emit(
                    SessionEventKind.DEBUGGER_STACK_OBSERVED,
                    {"pause_generation": self._pause_generation, "frames": ()},
                )
        if result is not None:
            official = result.get("official_verifier")
            if isinstance(official, Mapping):
                f2p_passed, f2p_total, p2p_passed, p2p_total = _official_verifier_counts(official)
                verifier_payload: dict[str, Any] = {
                    "status": "COMPLETED",
                    "outcome": "RESOLVED" if accepted else None,
                    "f2p_passed": f2p_passed,
                    "f2p_total": f2p_total,
                    "p2p_passed": p2p_passed,
                    "p2p_total": p2p_total,
                    "workspace_cleaned": cleanup,
                    "classification": _safe_text(classification, 256) if classification else None,
                }
                execution_proven = official.get("official_test_execution_proven")
                if type(execution_proven) is bool:
                    verifier_payload["official_test_execution_proven"] = execution_proven
                self._emit(SessionEventKind.VERIFIER_COMPLETED, verifier_payload)
        if result is None or isinstance(operator_failure, Mapping):
            if isinstance(operator_failure, Mapping):
                reason = _safe_text(operator_failure.get("message"), 512)
                detail = f"Level-32 operator failed — exit {returncode}: {reason or 'structured operator failure'}"
            elif self._result_parse_error:
                detail = f"Level-32 result parse failed — exit {returncode}: {self._result_parse_error}"
            else:
                reason = _safe_text(stderr or stdout, 512)
                detail = f"Level-32 operator failed — exit {returncode}: {reason or 'result.json was not produced'}"
            self._emit(
                SessionEventKind.DIAGNOSIS_RECORDED,
                {"text": detail, "file_path": None, "symbol": None, "confidence": "observed"},
            )
        self._emit(SessionEventKind.CLEANUP_STARTED, {})
        self._emit_progress(OperatorStage.CLEANUP)
        self._emit(SessionEventKind.CLEANUP_COMPLETED, {"verified": cleanup})
        if self._cancel_requested and not cleanup:
            status, reason, kind = SessionStatus.CLEANUP_FAILED, SessionTerminationReason.CLEANUP_FAILED, SessionEventKind.SESSION_FAILED
        elif self._cancel_requested:
            status, reason, kind = SessionStatus.CANCELLED, SessionTerminationReason.CANCELLED, SessionEventKind.SESSION_CANCELLED
        elif isinstance(operator_failure, Mapping) or result is None:
            status, reason, kind = SessionStatus.FAILED, SessionTerminationReason.SUBPROCESS_ERROR, SessionEventKind.SESSION_FAILED
        elif result is not None and accepted:
            status, reason, kind = SessionStatus.SUCCEEDED, SessionTerminationReason.DONE, SessionEventKind.SESSION_COMPLETED
        elif result is not None:
            status, reason, kind = SessionStatus.UNRESOLVED, SessionTerminationReason.UNRESOLVED, SessionEventKind.SESSION_COMPLETED
        else:
            status, reason, kind = SessionStatus.FAILED, SessionTerminationReason.SUBPROCESS_ERROR, SessionEventKind.SESSION_FAILED
        self._emit_progress(OperatorStage.COMPLETED)
        self._emit(
            kind,
            {"status": status.value, "termination_reason": reason.value},
        )
        diagnostics: tuple[str, ...] = ()
        if isinstance(operator_failure, Mapping):
            diagnostic = _safe_text(operator_failure.get("message"), 512)
            diagnostics = (diagnostic,) if diagnostic else ()
        elif result is None:
            diagnostic = _safe_text(stderr or stdout, 512)
            if self._result_parse_error:
                diagnostic = self._result_parse_error
            diagnostics = (diagnostic,) if diagnostic else ()
        self._result = SessionResult(
            session_id=SessionId(self._session_id),
            spec=self._spec,
            status=status,
            termination_reason=reason,
            run_id=self._run_id,
            started_at_utc=self._events[1].timestamp_utc,
            ended_at_utc=self._events[-1].timestamp_utc,
            sequence=self._events[-1].sequence,
            cleanup_verified=cleanup,
            diagnostics=diagnostics,
        )
        assert self._journal is not None
        self._journal.close()

    def wait(self) -> SessionResult:
        if self._thread is not None:
            self._thread.join()
        if self._result is None:
            raise RuntimeError("Level-32 operator ended without a terminal result")
        return self._result

    def cancel(self) -> None:
        self._cancel_requested = True
        process = self._process
        if process is not None and process.poll() is None:
            if isinstance(process, subprocess.Popen):
                try:
                    _terminate_command_tree(process)
                except Exception:
                    pass
            else:
                try:
                    process.terminate()
                    if process.poll() is None:
                        process.kill()
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.cancel()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._close_capture_streams()
        if self._journal is not None and self._result is None:
            self._journal.close()


def build_level32_spec(model_alias: str) -> SessionSpec:
    return SessionSpec(
        task_id=LEVEL32_TASK_ID,
        source=ExecutionSourceSpec(
            kind=SourceKind.LEVEL32_OPERATOR,
            task_id=LEVEL32_TASK_ID,
            policy="exact-pdb-level32-frozen",
            model_config_ref=model_alias,
        ),
        budgets=SessionBudgets(),
    )


__all__ = [
    "LEVEL32_TASK_ID",
    "LADDER_TASK_IDS",
    "LADDER_TASKS",
    "LadderTaskMetadata",
    "Level32ModelProfile",
    "Level32OperatorWorker",
    "build_level32_spec",
    "level32_model_profiles",
    "ladder_task_metadata",
    "ladder_task_options",
    "is_ladder_task",
    "ollama_cloud_model_profiles",
    "next_level32_treatment",
]
