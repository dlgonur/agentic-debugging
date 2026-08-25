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
from agentic_debugger.application.events import (
    SessionEvent,
    SessionEventKind,
    SessionPhase,
    SessionStatus,
    SessionTerminationReason,
    SourceKind,
    VerifierStage,
)
from agentic_debugger.application.history import HistoryStore
from agentic_debugger.application.journal import SessionEventJournal
from agentic_debugger.application.session import (
    SessionBudgets,
    SessionId,
    SessionResult,
    SessionSpec,
)
from agentic_debugger.application.sources import ExecutionSourceSpec

LEVEL32_TASK_ID = "audreyr__cookiecutter-967"
LEVEL32_OPERATOR_SCRIPT = "scripts/run_cookiecutter_967_pdb_proof.py"


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
    return text if len(text) <= maximum else text[: maximum - 3] + "..."


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

    @property
    def pid(self) -> Optional[int]:
        return self._process.pid if self._process is not None else None

    @property
    def events(self) -> Tuple[SessionEvent, ...]:
        with self._lock:
            return tuple(self._events)

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
            {"status": "running", "phase": SessionPhase.WAITING_MODEL.value},
        )
        argv = [
            sys.executable,
            str(self._repository_root / LEVEL32_OPERATOR_SCRIPT),
            "--repository-root", str(self._repository_root),
            "--output-dir", str(self._output_dir),
            "--model", self._model.alias,
            "--treatment-revision", str(self._revision),
            "--live",
            "--confirm-live-model-access",
        ]
        try:
            self._process = self._process_factory(
                argv,
                cwd=str(self._repository_root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
            )
        except Exception as exc:
            self._finish(returncode=2, stdout="", stderr=f"operator launch failed: {exc}", result=None)
            return None
        self._thread = threading.Thread(target=self._monitor, name="level32-operator", daemon=True)
        self._thread.start()
        return None

    def _monitor(self) -> None:
        assert self._process is not None
        try:
            stdout, stderr = self._process.communicate()
            returncode = self._process.returncode
        except Exception as exc:
            stdout, stderr, returncode = "", f"operator supervision failed: {exc}", 2
        result = self._read_result()
        self._finish(returncode=returncode if returncode is not None else 2, stdout=stdout, stderr=stderr, result=result)

    def _read_result(self) -> Optional[dict[str, Any]]:
        path = self._output_dir / "result.json"
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

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

    def _finish(self, *, returncode: int, stdout: str, stderr: str, result: Optional[dict[str, Any]]) -> None:
        if self._result is not None:
            return
        accepted = bool(result and result.get("accepted") is True)
        cleanup = bool(
            result
            and isinstance(result.get("cleanup"), Mapping)
            and result["cleanup"].get("temporary_source_removed") is True
            and result["cleanup"].get("private_official_material_removed") is True
        )
        classification = result.get("classification") if result else None
        if result is not None:
            self._record_artifacts()
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
        self._emit(SessionEventKind.CLEANUP_STARTED, {})
        self._emit(SessionEventKind.CLEANUP_COMPLETED, {"verified": cleanup})
        if self._cancel_requested:
            status, reason, kind = SessionStatus.CANCELLED, SessionTerminationReason.CANCELLED, SessionEventKind.SESSION_CANCELLED
        elif result is not None and accepted:
            status, reason, kind = SessionStatus.SUCCEEDED, SessionTerminationReason.DONE, SessionEventKind.SESSION_COMPLETED
        elif result is not None:
            status, reason, kind = SessionStatus.UNRESOLVED, SessionTerminationReason.UNRESOLVED, SessionEventKind.SESSION_COMPLETED
        else:
            status, reason, kind = SessionStatus.FAILED, SessionTerminationReason.SUBPROCESS_ERROR, SessionEventKind.SESSION_FAILED
        self._emit(
            kind,
            {"status": status.value, "termination_reason": reason.value},
        )
        diagnostics: tuple[str, ...] = ()
        if result is None:
            diagnostic = _safe_text(stderr or stdout)
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
            try:
                process.terminate()
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
            self._thread.join(timeout=60.0)
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
    "Level32ModelProfile",
    "Level32OperatorWorker",
    "build_level32_spec",
    "level32_model_profiles",
    "next_level32_treatment",
]
