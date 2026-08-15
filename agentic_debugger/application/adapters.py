"""Read-only adapters for explicitly supported historical evidence formats.

Task-5 historical adapters convert known recorded formats into validated
Task-1 :class:`SessionEvent` presentation streams without mutating the source
evidence:

- canonical scientific trajectories (``RunEvent`` 1.0 JSONL);
- accepted R5 evidence (``debugger-interaction-v2-r5-evidence`` JSON);
- professor-safe traces (``professor_debug_trace_v1`` JSON).

Rules:

- Original identifiers (run id, task id, hashes) are preserved verbatim in
  the produced events/info; the presentation ``session_id`` is derived
  deterministically (the recorded run id when it is a valid session id,
  otherwise a digest-based presentation id).
- ``run_id`` is populated ONLY when the source evidence actually records a
  genuine run/session identifier (Repair Pass 2).  A Git source commit
  (``source_commit_sha``) or an experiment id (``experiment_id``) is
  source/experiment provenance, never run identity: those stay in
  ``RecordedRunInfo.provenance`` and never collapse distinct tasks into one
  presentation session.
- Recorded hashes/provenance are retained; absent source, locals, patch
  bodies, or verifier data stay ``NOT RECORDED`` (``None``/empty) and are
  never reconstructed from the current repository.
- Historical scientific outcomes are never reclassified: the recorded
  verifier status/outcome is copied verbatim into presentation events.
- The source evidence is never written, renamed, moved, appended to, or
  re-hashed; the adapters read only.
- No adapter executes the recorded run.

Other heterogeneous experiment/campaign evidence is intentionally NOT
recursively scanned or forced into one schema; it remains open-by-path
functionality for later work.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from agentic_debugger import SchemaValidationError
from agentic_debugger.agent.state_machine import ControllerState
from agentic_debugger.application import ApplicationInputError
from agentic_debugger.application.events import (
    SESSION_EVENT_SCHEMA_VERSION,
    SessionEvent,
    SessionEventKind,
    SourceKind,
    validate_session_id,
    validate_utc_timestamp,
)
from agentic_debugger.application.presentation import PresentationIdentity
from agentic_debugger.events.replay import ReplayError, replay_events
from agentic_debugger.events.schema import EventType, ObservationStatus

#: Recorded source kinds for adapted historical material (replay-only).
RECORDED_SOURCE_KINDS = {
    "canonical_trajectory": SourceKind.CANONICAL_TRAJECTORY,
    "r5_evidence": SourceKind.EXPERIMENT_EVIDENCE,
    "professor_trace": SourceKind.EXPERIMENT_EVIDENCE,
}

_MAX_TEXT_BYTES = 512
_MAX_IDENTIFIER_BYTES = 256
_MAX_STACK_FRAMES = 64
_MAX_PROVENANCE_PAIRS = 16
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class AdapterError(ApplicationInputError):
    """Raised when recorded evidence cannot be adapted safely."""


@dataclass(frozen=True)
class RecordedRunInfo:
    """Preserved identity/provenance of one adapted recorded source.

    ``run_id`` is populated ONLY when the source evidence actually records a
    genuine run/session identifier (Repair Pass 2): a Git source commit or
    an experiment id is source/experiment provenance, never run identity.
    ``provenance`` carries that bounded recorded provenance (ordered
    ``(key, value)`` pairs such as ``source_commit_sha`` / ``experiment_id``)
    so it is preserved without overloading ``run_id``.
    """

    format: str
    source_kind: SourceKind
    session_id: str
    path: str
    sha256: str
    task_id: Optional[str] = None
    run_id: Optional[str] = None
    note: Optional[str] = None
    provenance: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if type(self.provenance) is not tuple or len(self.provenance) > _MAX_PROVENANCE_PAIRS:
            raise AdapterError(
                f"provenance must be a bounded tuple of at most "
                f"{_MAX_PROVENANCE_PAIRS} pairs"
            )
        normalized: list[tuple[str, str]] = []
        for index, pair in enumerate(self.provenance):
            if type(pair) is not tuple or len(pair) != 2:
                raise AdapterError(f"provenance[{index}] must be a (key, value) pair")
            key = _bounded_text(pair[0], f"provenance[{index}] key", _MAX_IDENTIFIER_BYTES)
            value = _bounded_text(pair[1], f"provenance[{index}] value", _MAX_IDENTIFIER_BYTES)
            normalized.append((key, value))
        object.__setattr__(self, "provenance", tuple(normalized))

    def provenance_mapping(self) -> Dict[str, str]:
        """The recorded provenance as a plain mapping (last pair wins)."""
        return {key: value for key, value in self.provenance}


@dataclass(frozen=True)
class RecordedSource:
    """One adapted recorded source: presentation identity + events + info."""

    identity: PresentationIdentity
    events: Tuple[SessionEvent, ...]
    info: RecordedRunInfo


def _default_clock() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validated_clock(clock: Callable[[], str]) -> Callable[[], str]:
    if not callable(clock):
        raise ApplicationInputError("clock must be callable")
    try:
        validate_utc_timestamp(clock())
    except Exception as exc:
        raise ApplicationInputError("clock must produce UTC timestamps") from exc
    return clock


def _bounded_text(value: Any, label: str, max_chars: int) -> str:
    if type(value) is not str or not value:
        raise AdapterError(f"{label} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise AdapterError(f"{label} must be UTF-8 text")
    if len(encoded) > max_chars:
        raise AdapterError(f"{label} exceeds the {max_chars}-byte bound")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise AdapterError(f"{label} contains control characters")
    return value


def _bounded_text_or_none(value: Any, label: str, max_chars: int) -> Optional[str]:
    if value is None:
        return None
    return _bounded_text(value, label, max_chars)


def _nonneg_int(value: Any, label: str) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 0:
        raise AdapterError(f"{label} must be a non-negative integer")
    return value


def _hex64_or_none(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    if type(value) is not str or _SHA256_HEX_RE.match(value) is None:
        return None
    return value


def derive_recorded_session_id(
    run_id: Optional[str], task_id: Optional[str], path: str = ""
) -> str:
    """One deterministic presentation session id for recorded material.

    Uses the recorded run id when it is already a valid session id (the
    presentation then shows the original identity); otherwise derives a
    stable ``recorded-<digest>`` id from the recorded identity and path.
    """
    if run_id is not None:
        try:
            validate_session_id(run_id)
            return run_id
        except Exception:
            pass
    material = "\x00".join(
        part for part in (run_id or "", task_id or "", path) if part
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"recorded-{digest}"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _EventBuilder:
    """Small internal event emitter with a fixed identity and sequence."""

    def __init__(
        self,
        *,
        session_id: str,
        task_id: str,
        source_kind: SourceKind,
        run_id: Optional[str],
        clock: Callable[[], str],
    ) -> None:
        self._session_id = session_id
        self._task_id = task_id
        self._source_kind = source_kind
        self._run_id = run_id
        self._clock = clock
        self._sequence = 0
        self._events: list[SessionEvent] = []

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def source_kind(self) -> SourceKind:
        return self._source_kind

    @property
    def events(self) -> Tuple[SessionEvent, ...]:
        return tuple(self._events)

    def emit(
        self,
        kind: SessionEventKind,
        payload: Dict[str, Any],
        *,
        controller_phase: Optional[ControllerState] = None,
        timestamp: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> SessionEvent:
        event = SessionEvent(
            schema_version=SESSION_EVENT_SCHEMA_VERSION,
            session_id=self._session_id,
            task_id=self._task_id,
            run_id=run_id if run_id is not None else self._run_id,
            sequence=self._sequence,
            timestamp_utc=timestamp if timestamp is not None else self._clock(),
            source_kind=self._source_kind,
            event_kind=kind,
            controller_phase=controller_phase,
            payload=payload,
        )
        self._sequence += 1
        self._events.append(event)
        return event


def _state_or_none(value: Any) -> Optional[ControllerState]:
    if value is None:
        return None
    try:
        return ControllerState(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Canonical trajectory adapter
# ---------------------------------------------------------------------------


def _map_canonical_event(
    builder: _EventBuilder,
    event: Any,
    decision_count: List[int],
    timestamp: str,
) -> None:
    """Map one validated canonical RunEvent into app presentation events."""
    controller_phase = _state_or_none(getattr(event, "state", None))
    event_type = getattr(event, "event_type", None)
    name = getattr(event, "name", "") or ""
    payload = dict(getattr(event, "payload", None) or {})
    if event_type is EventType.DECISION:
        builder.emit(
            SessionEventKind.CONTROLLER_STEP,
            {
                "step_index": decision_count[0],
                "directive_kind": _bounded_text_or_none(
                    payload.get("directive_kind"), "directive_kind", 64
                ),
                "stop_reason": _bounded_text_or_none(
                    payload.get("stop_reason"), "stop_reason", 64
                ),
            },
            controller_phase=controller_phase,
            timestamp=timestamp,
        )
        decision_count[0] += 1
        return
    if event_type is EventType.ACTION:
        builder.emit(
            SessionEventKind.TOOL_STARTED,
            {"tool_name": _bounded_text(name, "action name", _MAX_IDENTIFIER_BYTES)},
            controller_phase=controller_phase,
            timestamp=timestamp,
        )
        return
    if event_type is EventType.OBSERVATION:
        observation = payload.get("observation")
        status: Optional[str] = None
        if isinstance(observation, dict):
            raw_status = observation.get("status")
            status = raw_status.value if hasattr(raw_status, "value") else raw_status
        try:
            validated_status = ObservationStatus(status) if status is not None else None
        except ValueError:
            validated_status = None
        if validated_status is None:
            # A schema-valid canonical observation always carries a usable
            # status; without one the completion is NOT RECORDED rather than
            # fabricated.
            return
        builder.emit(
            SessionEventKind.TOOL_COMPLETED,
            {
                "tool_name": _bounded_text(name, "observation name", _MAX_IDENTIFIER_BYTES),
                "status": validated_status.value,
            },
            controller_phase=controller_phase,
            timestamp=timestamp,
        )
        return
    if event_type is EventType.TRANSITION:
        source_state = payload.get("source_state")
        target_state = payload.get("target_state")
        if not isinstance(source_state, str) or not isinstance(target_state, str):
            return
        builder.emit(
            SessionEventKind.CONTROLLER_TRANSITION,
            {
                "source_state": source_state,
                "target_state": target_state,
                "reason": _bounded_text_or_none(
                    payload.get("reason"), "reason", 4096
                ),
            },
            controller_phase=controller_phase,
            timestamp=timestamp,
        )
        return
    if event_type is EventType.FINAL:
        builder.emit(
            SessionEventKind.CONTROLLER_STEP,
            {
                "step_index": max(0, decision_count[0]),
                "directive_kind": None,
                "stop_reason": _bounded_text_or_none(
                    payload.get("stop_reason"), "stop_reason", 64
                ),
            },
            controller_phase=controller_phase,
            timestamp=timestamp,
        )
        return


def adapt_canonical_trajectory(
    source: Any,
    *,
    path: str = "",
    clock: Callable[[], str] | None = None,
) -> RecordedSource:
    """Adapt one canonical ``RunEvent`` 1.0 JSONL trajectory read-only.

    ``source`` is JSONL text or a validated ``ReplayTrajectory``.  Every
    canonical decision/action/observation/transition/final record is mapped
    into presentation events; original run/task ids are preserved and the
    canonical event IDs remain in the source (never rewritten).
    """
    validated_clock = _validated_clock(clock) if clock is not None else _default_clock
    try:
        trajectory = replay_events(source)
    except ReplayError as exc:
        raise AdapterError(f"canonical trajectory is invalid: {exc}") from exc
    run_id = _bounded_text(trajectory.run_id, "run_id", _MAX_IDENTIFIER_BYTES)
    task_id = _bounded_text(trajectory.task_id, "task_id", _MAX_IDENTIFIER_BYTES)
    session_id = derive_recorded_session_id(run_id, task_id, path)
    builder = _EventBuilder(
        session_id=session_id,
        task_id=task_id,
        source_kind=RECORDED_SOURCE_KINDS["canonical_trajectory"],
        run_id=run_id,
        clock=validated_clock,
    )
    decision_count: List[int] = [0]
    canonical_events = trajectory.events
    # Canonical projections share one recorded timestamp; reuse it when valid.
    recorded_timestamp = (
        canonical_events[0].timestamp if canonical_events else None
    )
    try:
        timestamp = (
            validate_utc_timestamp(recorded_timestamp)
            if recorded_timestamp is not None
            else validated_clock()
        )
    except Exception:
        timestamp = validated_clock()
    for event in canonical_events:
        _map_canonical_event(builder, event, decision_count, timestamp)
    info = RecordedRunInfo(
        format="canonical_trajectory",
        source_kind=builder.source_kind,
        session_id=session_id,
        path=path,
        sha256="",
        task_id=task_id,
        run_id=run_id,
    )
    return RecordedSource(
        identity=PresentationIdentity(
            task_id=task_id, source_kind=info.source_kind, session_id=session_id
        ),
        events=builder.events,
        info=info,
    )


# ---------------------------------------------------------------------------
# Professor trace adapter
# ---------------------------------------------------------------------------


def _trace_frames(entry: Any) -> Tuple[Dict[str, Any], ...]:
    frames = entry.get("frames") if isinstance(entry, dict) else None
    if type(frames) is not list:
        return ()
    normalized: list[Dict[str, Any]] = []
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            continue
        try:
            normalized.append(
                {
                    "index": index,
                    "function": _bounded_text(
                        frame.get("function") or "", f"trace frame {index} function",
                        _MAX_IDENTIFIER_BYTES,
                    ),
                    "file": _bounded_text(
                        frame.get("file") or "", f"trace frame {index} file",
                        _MAX_TEXT_BYTES,
                    ),
                    "line": _nonneg_int(frame.get("line") or 0, f"trace frame {index} line"),
                    "is_current": frame.get("is_current") is True,
                }
            )
        except AdapterError:
            continue
    return tuple(normalized[: _MAX_STACK_FRAMES])


def adapt_professor_trace(
    mapping: Any,
    *,
    path: str = "",
    clock: Callable[[], str] | None = None,
) -> RecordedSource:
    """Adapt one professor-safe trace (``professor_debug_trace_v1``) read-only.

    The trace already omits source, locals, hidden tests, and oracle fields;
    the adapter preserves those omissions (``NOT RECORDED``).  The recorded
    diagnosis text, debugger trace entries, repair attempt hashes, and final
    verification are mapped verbatim.
    """
    if not isinstance(mapping, dict):
        raise AdapterError("professor trace must be a JSON mapping")
    schema = mapping.get("schema_version")
    if schema != "professor_debug_trace_v1":
        raise AdapterError(f"unsupported professor trace schema: {schema!r}")
    validated_clock = _validated_clock(clock) if clock is not None else _default_clock
    task_id = _bounded_text_or_none(mapping.get("task_id"), "task_id", _MAX_IDENTIFIER_BYTES) or "unknown"
    # Repair Pass 2: the professor trace records no genuine run/session
    # identifier -- ``run_provenance.source_commit_sha`` is a Git source
    # commit (source provenance), never a run id.  ``run_id`` therefore
    # stays None; the commit is preserved as provenance and the presentation
    # session id is derived deterministically from the task/path identity.
    provenance: list[tuple[str, str]] = []
    source_commit = None
    run_provenance = mapping.get("run_provenance")
    if isinstance(run_provenance, dict) and run_provenance.get("source_commit_sha"):
        source_commit = _bounded_text_or_none(
            run_provenance.get("source_commit_sha"),
            "source_commit_sha",
            _MAX_IDENTIFIER_BYTES,
        )
        provenance.append(("source_commit_sha", source_commit))
    run_id = None
    session_id = derive_recorded_session_id(None, task_id, path)
    builder = _EventBuilder(
        session_id=session_id,
        task_id=task_id,
        source_kind=RECORDED_SOURCE_KINDS["professor_trace"],
        run_id=run_id,
        clock=validated_clock,
    )
    diagnosis = mapping.get("diagnosis")
    if isinstance(diagnosis, dict) and diagnosis.get("model_authored") is True:
        text = diagnosis.get("text")
        if text:
            builder.emit(
                SessionEventKind.DIAGNOSIS_RECORDED,
                {
                    "text": _bounded_text(text, "diagnosis text", 4096),
                    "file_path": None,
                    "symbol": None,
                    "confidence": None,
                },
            )
    localization = mapping.get("error_localization")
    if isinstance(localization, dict):
        file_path = localization.get("production_file")
        line = localization.get("line_or_region")
        function = localization.get("function")
        generation = localization.get("pause_generation")
        if isinstance(file_path, str) and file_path:
            builder.emit(
                SessionEventKind.DEBUGGER_LOCATION_CHANGED,
                {
                    "script": _bounded_text(file_path, "localization file", _MAX_TEXT_BYTES),
                    "line": line if type(line) is int and line >= 1 else None,
                    "function": _bounded_text_or_none(
                        function, "localization function", _MAX_IDENTIFIER_BYTES
                    ),
                    # An unrecorded pause generation stays NOT RECORDED
                    # (null); it is never synthesized as recorded evidence.
                    "pause_generation": (
                        _nonneg_int(generation, "pause_generation")
                        if type(generation) is int
                        else None
                    ),
                },
            )
    trace_entries = mapping.get("debugger_trace")
    if isinstance(trace_entries, list):
        for entry in trace_entries:
            if not isinstance(entry, dict):
                continue
            file_path = entry.get("production_file")
            line = entry.get("line")
            function = entry.get("function")
            generation = entry.get("pause_generation")
            # Recorded generations are preserved verbatim; an entry without
            # one keeps ``None`` (NOT RECORDED) -- synthetic generation
            # counters are never presented as historically recorded.
            current_generation = (
                generation if type(generation) is int and generation >= 0 else None
            )
            frames = _trace_frames(entry)
            if frames:
                builder.emit(
                    SessionEventKind.DEBUGGER_STACK_OBSERVED,
                    {
                        "pause_generation": current_generation,
                        "frames": frames,
                    },
                )
            if isinstance(file_path, str) and file_path:
                builder.emit(
                    SessionEventKind.DEBUGGER_LOCATION_CHANGED,
                    {
                        "script": _bounded_text(file_path, "trace file", _MAX_TEXT_BYTES),
                        "line": line if type(line) is int and line >= 1 else None,
                        "function": _bounded_text_or_none(
                            function, "trace function", _MAX_IDENTIFIER_BYTES
                        ),
                        "pause_generation": current_generation,
                    },
                )
    attempts = mapping.get("repair_attempts")
    if isinstance(attempts, list):
        for index, attempt in enumerate(attempts):
            if not isinstance(attempt, dict):
                continue
            sha = _hex64_or_none(
                attempt.get("model_patch_serialization_normalized_sha256")
                or attempt.get("model_patch_raw_sha256"),
                f"attempt {index} sha256",
            )
            if sha is None:
                continue
            builder.emit(
                SessionEventKind.PATCH_PROPOSED,
                {"attempt_index": index, "patch_sha256": sha},
            )
    verification = mapping.get("final_verification")
    if isinstance(verification, dict):
        # An unrecorded workspace lifecycle is unknown (NOT RECORDED), not a
        # recorded "not cleaned" failure; only a recorded lifecycle value is
        # mapped to a boolean.
        workspace_lifecycle = verification.get("workspace_lifecycle")
        builder.emit(
            SessionEventKind.VERIFIER_COMPLETED,
            {
                "status": _bounded_text_or_none(
                    verification.get("verifier_status"), "verifier status", 64
                ),
                "outcome": _bounded_text_or_none(
                    verification.get("outcome"), "verifier outcome", 64
                ),
                "f2p_passed": _count_pair(verification.get("f2p"), 0),
                "f2p_total": _count_pair(verification.get("f2p"), 1),
                "p2p_passed": _count_pair(verification.get("p2p"), 0),
                "p2p_total": _count_pair(verification.get("p2p"), 1),
                "workspace_cleaned": (
                    None
                    if workspace_lifecycle is None
                    else (workspace_lifecycle == "CLEANED")
                ),
            },
        )
    return RecordedSource(
        identity=PresentationIdentity(
            task_id=task_id, source_kind=builder.source_kind, session_id=session_id
        ),
        events=builder.events,
        info=RecordedRunInfo(
            format="professor_trace",
            source_kind=builder.source_kind,
            session_id=session_id,
            path=path,
            sha256="",
            task_id=task_id,
            run_id=None,
            provenance=tuple(provenance),
        ),
    )


def _count_pair(value: Any, index: int) -> Optional[int]:
    """Parse ``"1/1"``-style recorded counts into the requested side."""
    if type(value) is not str:
        return None
    parts = value.split("/")
    if len(parts) != 2:
        return None
    try:
        number = int(parts[index].strip())
    except ValueError:
        return None
    return number if number >= 0 else None


# ---------------------------------------------------------------------------
# R5 evidence adapter
# ---------------------------------------------------------------------------


def adapt_r5_evidence(
    mapping: Any,
    *,
    path: str = "",
    clock: Callable[[], str] | None = None,
) -> RecordedSource:
    """Adapt one R5 evidence document read-only.

    Reuses the canonical trajectory adapter over the embedded
    ``trajectory_jsonl`` and adds the recorded verifier, patch identity, and
    diagnosis facts.  Original identifiers and hashes are preserved; absent
    fields stay ``NOT RECORDED``.
    """
    if not isinstance(mapping, dict):
        raise AdapterError("r5 evidence must be a JSON mapping")
    schema = mapping.get("schema_version")
    if schema != "debugger-interaction-v2-r5-evidence":
        raise AdapterError(f"unsupported r5 evidence schema: {schema!r}")
    validated_clock = _validated_clock(clock) if clock is not None else _default_clock
    task_meta = mapping.get("task")
    task_id = "unknown"
    if isinstance(task_meta, dict) and task_meta.get("task_id"):
        task_id = _bounded_text(
            task_meta.get("task_id"), "task_id", _MAX_IDENTIFIER_BYTES
        )
    # Repair Pass 2: ``run_identity.experiment_id`` identifies the whole
    # experiment/matrix (``debugger-interaction-v2-r5``), never one task
    # execution -- it is experiment provenance, not a run id.  The genuine
    # per-task run identifier recorded by the evidence lives in the embedded
    # canonical trajectory's run id and is preserved verbatim when present.
    run_id: Optional[str] = None
    trajectory_jsonl = mapping.get("trajectory_jsonl")
    if isinstance(trajectory_jsonl, str) and trajectory_jsonl.strip():
        try:
            trajectory = replay_events(trajectory_jsonl)
        except ReplayError as exc:
            raise AdapterError(f"embedded r5 trajectory is invalid: {exc}") from exc
        if trajectory.run_id:
            run_id = _bounded_text(
                trajectory.run_id, "trajectory run_id", _MAX_IDENTIFIER_BYTES
            )
    else:
        trajectory = None
    provenance: list[tuple[str, str]] = []
    run_identity = mapping.get("run_identity")
    if isinstance(run_identity, dict):
        experiment_id = run_identity.get("experiment_id")
        if experiment_id:
            provenance.append(
                (
                    "experiment_id",
                    _bounded_text(experiment_id, "experiment_id", _MAX_IDENTIFIER_BYTES),
                )
            )
        source_commit = run_identity.get("source_commit_sha")
        if source_commit:
            provenance.append(
                (
                    "source_commit_sha",
                    _bounded_text(source_commit, "source_commit_sha", _MAX_IDENTIFIER_BYTES),
                )
            )
    session_id = derive_recorded_session_id(run_id, task_id, path)
    builder = _EventBuilder(
        session_id=session_id,
        task_id=task_id,
        source_kind=RECORDED_SOURCE_KINDS["r5_evidence"],
        run_id=run_id,
        clock=validated_clock,
    )
    if trajectory is not None:
        recorded_timestamp = (
            trajectory.events[0].timestamp if trajectory.events else None
        )
        try:
            timestamp = (
                validate_utc_timestamp(recorded_timestamp)
                if recorded_timestamp is not None
                else validated_clock()
            )
        except Exception:
            timestamp = validated_clock()
        decision_count: List[int] = [0]
        for event in trajectory.events:
            _map_canonical_event(builder, event, decision_count, timestamp)
    # A diagnosis presentation fact exists only when the evidence actually
    # records a diagnosis claim (``post_debug_diagnoses`` carries the
    # recorded model-authored text).  Provenance metadata alone
    # (``diagnosis_provenance``) is not a diagnosis artifact: an empty or
    # hash-only provenance block must never create a timeline fact that did
    # not exist.
    post_diagnoses = mapping.get("post_debug_diagnoses")
    if isinstance(post_diagnoses, list):
        for entry in post_diagnoses:
            if not isinstance(entry, dict):
                continue
            text = entry.get("text")
            if type(text) is not str or not text:
                continue
            builder.emit(
                SessionEventKind.DIAGNOSIS_RECORDED,
                {
                    "text": _bounded_text(text, "diagnosis text", 4096),
                    "file_path": None,
                    "symbol": None,
                    "confidence": None,
                },
            )
    attempts = (mapping.get("patch_identity") or {}).get("attempts")
    if isinstance(attempts, list):
        for index, attempt in enumerate(attempts):
            if not isinstance(attempt, dict):
                continue
            sha = _hex64_or_none(
                attempt.get("model_patch_serialization_normalized_sha256")
                or attempt.get("model_patch_raw_sha256"),
                f"attempt {index} sha256",
            )
            if sha is None:
                continue
            builder.emit(
                SessionEventKind.PATCH_PROPOSED,
                {"attempt_index": index, "patch_sha256": sha},
            )
    verifier = mapping.get("verifier")
    if isinstance(verifier, dict) and verifier.get("executed") is True:
        # An unrecorded workspace lifecycle is unknown (NOT RECORDED), not a
        # recorded "not cleaned" failure.
        workspace_lifecycle = verifier.get("workspace_lifecycle")
        builder.emit(
            SessionEventKind.VERIFIER_COMPLETED,
            {
                "status": _bounded_text_or_none(
                    verifier.get("status"), "verifier status", 64
                ),
                "outcome": _bounded_text_or_none(
                    verifier.get("outcome"), "verifier outcome", 64
                ),
                "f2p_passed": _int_or_none(verifier.get("f2p_passed")),
                "f2p_total": _int_or_none(verifier.get("f2p_total")),
                "p2p_passed": _int_or_none(verifier.get("p2p_passed")),
                "p2p_total": _int_or_none(verifier.get("p2p_total")),
                "workspace_cleaned": (
                    None
                    if workspace_lifecycle is None
                    else (workspace_lifecycle == "CLEANED")
                ),
            },
        )
    return RecordedSource(
        identity=PresentationIdentity(
            task_id=task_id, source_kind=builder.source_kind, session_id=session_id
        ),
        events=builder.events,
        info=RecordedRunInfo(
            format="r5_evidence",
            source_kind=builder.source_kind,
            session_id=session_id,
            path=path,
            sha256="",
            task_id=task_id,
            run_id=run_id,
            provenance=tuple(provenance),
        ),
    )


def _int_or_none(value: Any) -> Optional[int]:
    if type(value) is not int or isinstance(value, bool) or value < 0:
        return None
    return value


# ---------------------------------------------------------------------------
# Format detection and dispatch
# ---------------------------------------------------------------------------


def detect_recorded_format(text: str) -> str:
    """Detect the explicit recorded format from a file's content.

    Only the known supported formats are recognized; anything else fails
    closed (``unknown``) so heterogeneous evidence is never forced into one
    schema.
    """
    stripped = text.lstrip()
    # Canonical trajectories are JSONL (one RunEvent object per line), so
    # the event_type marker must be checked before the single-object branch.
    if '"event_type"' in stripped:
        return "canonical_trajectory"
    if stripped.startswith("{"):
        try:
            mapping = json.loads(stripped)
        except (ValueError, TypeError):
            return "unknown"
        if isinstance(mapping, dict):
            schema = mapping.get("schema_version")
            if schema == "professor_debug_trace_v1":
                return "professor_trace"
            if schema == "debugger-interaction-v2-r5-evidence":
                return "r5_evidence"
        return "unknown"
    return "unknown"


def open_recorded_file(
    path: str | Path,
    *,
    format: Optional[str] = None,
    clock: Callable[[], str] | None = None,
) -> RecordedSource:
    """Open one explicitly supported recorded evidence file read-only.

    The file is never modified; its SHA-256 is preserved in the returned
    info.  ``format`` may be supplied explicitly (``canonical_trajectory``,
    ``professor_trace``, ``r5_evidence``) or auto-detected; unknown formats
    fail closed.
    """
    source_path = Path(path)
    if not source_path.is_file():
        raise AdapterError(f"recorded file does not exist: {source_path}")
    try:
        text = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AdapterError(f"cannot read recorded file: {exc}") from exc
    detected = detect_recorded_format(text)
    chosen = format if format is not None else detected
    if chosen == "unknown":
        raise AdapterError(
            f"unsupported or unrecognized recorded format: {detected!r}"
        )
    if format is not None and format != detected:
        raise AdapterError(
            f"declared format {format!r} does not match detected {detected!r}"
        )
    validated_clock = _validated_clock(clock) if clock is not None else _default_clock
    if chosen == "canonical_trajectory":
        adapted = adapt_canonical_trajectory(
            text, path=str(source_path), clock=validated_clock
        )
    elif chosen == "professor_trace":
        try:
            mapping = json.loads(text)
        except ValueError as exc:
            raise AdapterError(f"professor trace is not valid JSON: {exc}") from exc
        adapted = adapt_professor_trace(
            mapping, path=str(source_path), clock=validated_clock
        )
    else:
        try:
            mapping = json.loads(text)
        except ValueError as exc:
            raise AdapterError(f"r5 evidence is not valid JSON: {exc}") from exc
        adapted = adapt_r5_evidence(
            mapping, path=str(source_path), clock=validated_clock
        )
    info = RecordedRunInfo(
        format=adapted.info.format,
        source_kind=adapted.info.source_kind,
        session_id=adapted.info.session_id,
        path=str(source_path),
        sha256=_file_sha256(source_path),
        task_id=adapted.info.task_id,
        run_id=adapted.info.run_id,
        note=adapted.info.note,
        provenance=adapted.info.provenance,
    )
    return RecordedSource(
        identity=adapted.identity, events=adapted.events, info=info
    )


__all__ = [
    "AdapterError",
    "RecordedRunInfo",
    "RecordedSource",
    "adapt_canonical_trajectory",
    "adapt_professor_trace",
    "adapt_r5_evidence",
    "derive_recorded_session_id",
    "detect_recorded_format",
    "open_recorded_file",
]
