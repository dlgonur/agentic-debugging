"""Application-facing producer for debugger/source/patch/diagnosis events.

This is the Task-4 observability boundary: it converts structured facts the
real execution system already owns into validated Task-1 :class:`SessionEvent`
values.  It is the debugger/patch/source counterpart of the Task-2 controller
adapter (:mod:`agentic_debugger.application.controller_adapter`).

Boundary rules:

- This adapter maps only facts the real execution system authoritatively
  owns: structured PDB results (started location, location changes, stack
  summaries, frame locals), the real patch lifecycle (proposed/rejected/
  apply-failed/applied/reverted), safe source snapshots, and explicitly
  recorded diagnosis artifacts.  It never re-parses PDB terminal text and
  never invents PDB, patch, verifier, or session lifecycle facts.
- It is NOT the session lifecycle owner and never fabricates
  ``session.*`` / ``cleanup.*`` / verifier terminal events.
- It is NOT the verifier observer (see :mod:`application.verifier_observer`).
- Identity is fail-closed: every emitted event carries the configured
  session/task/run identity and a contiguous sequence owned by this adapter
  (starting at ``initial_sequence``, the caller's offset in the session's
  single sequence space).
- Only live-startable Task-1 source kinds are accepted, mirroring the
  controller adapter and the Task-1 authority ``can_start_new_session``.
- ``pause_generation`` values are the producer's responsibility: the demo
  tool context tracks the real PDB worker's generation (the stack summary
  reports it authoritatively).  The presentation reducer refuses stale
  stack/locals observations for older generations.

Import note: this module imports only the application contract modules and
runtime-independent mapping helpers; it loads no controller/PDB/verifier
execution code.  The PDB result mappings it consumes are plain validated
dicts produced by ``runtime.pdb_session``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from agentic_debugger.application import (
    ApplicationContractError,
    ApplicationInputError,
)
from agentic_debugger.application.events import (
    SessionEvent,
    SessionEventKind,
    SourceKind,
    contains_credential_shape,
    is_credential_name,
    validate_session_id,
    validate_utc_timestamp,
)
from agentic_debugger.application.emitter import SessionEventEmitter
from agentic_debugger.application.sources import SessionEventSink, can_start_new_session

_MAX_TASK_ID_BYTES = 256
_MAX_RUN_ID_BYTES = 256
_MAX_SHORT_TEXT_BYTES = 64
_MAX_NAME_BYTES = 256
_MAX_SUMMARY_BYTES = 512
_MAX_STACK_FRAMES = 64
_MAX_LOCALS = 512

#: Explicit redaction marker for a runtime local whose *name* is
#: credential-shaped (``api_key``, ``access_token``, ``authorization``,
#: ``credential``, ``password``, ``secret``, ``token``).  The variable name
#: is preserved; the summarized value is never exposed and is never silently
#: replaced with a fake ordinary value (Repair Pass 2).
_REDACTED_LOCAL_SUMMARY = "<redacted: credential-shaped local name>"

#: Kind -> summary renderer used for bounded PDB frame-locals summaries.
_VALUE_KIND_HANDLERS: Dict[str, Callable[[Mapping[str, Any]], str]] = {}


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
    if type(value) is not str or not value or value != value.strip():
        raise ApplicationInputError(f"{label} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ApplicationInputError(f"{label} must be UTF-8 text")
    if len(encoded) > max_chars:
        raise ApplicationInputError(f"{label} exceeds the {max_chars}-byte bound")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ApplicationInputError(f"{label} contains control characters")
    return value


def _bounded_text_or_none(value: Any, label: str, max_chars: int) -> Optional[str]:
    if value is None:
        return None
    return _bounded_text(value, label, max_chars)


def _multiline_text_or_none(value: Any, label: str, max_chars: int) -> Optional[str]:
    """Bounded text that may contain normal whitespace (diagnosis claims)."""
    if value is None:
        return None
    if type(value) is not str:
        raise ApplicationInputError(f"{label} must be a string or null")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ApplicationInputError(f"{label} must be UTF-8 text")
    if len(encoded) > max_chars:
        raise ApplicationInputError(f"{label} exceeds the {max_chars}-byte bound")
    for char in value:
        code = ord(char)
        if code == 0x00 or code == 0x7F or (code < 0x20 and char not in "\n\t\r"):
            raise ApplicationInputError(f"{label} contains a prohibited control character")
    return value


def _bounded_diagnostic(value: str) -> str:
    """Bound one rejection/apply-failure diagnostic for an event payload."""
    return _multiline_text_or_none(value, "diagnostic", 512) or ""


def _nonneg_int(value: Any, label: str) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 0:
        raise ApplicationInputError(f"{label} must be a non-negative integer")
    return value


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 1:
        raise ApplicationInputError(f"{label} must be a positive integer")
    return value


def _bounded_summary(value: str) -> str:
    """Truncate one local-value summary to the event bound."""
    if len(value.encode("utf-8", errors="replace")) <= _MAX_SUMMARY_BYTES:
        return value
    return (
        value.encode("utf-8", errors="replace")[:_MAX_SUMMARY_BYTES - 3].decode(
            "utf-8", errors="replace"
        )
        + "..."
    )


def summarize_value_summary(summary: Mapping[str, Any]) -> str:
    """Render one structured PDB value summary into a bounded scalar string.

    The PDB worker already validates value summaries; this projection keeps
    the app event small and safe (scalars as their text value, containers as
    a size summary, objects as their type).  Never executes any code and
    never renders raw memory content beyond the PDB's own previews.
    """
    if not isinstance(summary, Mapping):
        return "<unknown>"
    kind = summary.get("kind")
    if kind == "none":
        return "None"
    if kind == "bool":
        value = summary.get("value")
        return "True" if value is True else "False"
    if kind in ("int", "float"):
        value = summary.get("value")
        if value is not None:
            return _bounded_summary(str(value))
        special = summary.get("special")
        return _bounded_summary(str(special)) if special is not None else "<value>"
    if kind == "str":
        return _bounded_summary(str(summary.get("value") or ""))
    if kind == "bytes":
        value = summary.get("value")
        if value:
            return _bounded_summary("<bytes:" + str(value) + ">")
        return "<bytes>"
    if kind in ("list", "tuple", "set", "frozenset", "dict"):
        size = summary.get("size")
        return f"<{kind} size={size if size is not None else '?'}>"
    if kind == "object":
        type_name = summary.get("type")
        return _bounded_summary(f"<{type_name}>") if type_name else "<object>"
    return "<value>"


def _normalized_frames(stack_result: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    frames = stack_result.get("frames")
    if type(frames) is not list:
        raise ApplicationInputError("stack result frames must be a list")
    if len(frames) > _MAX_STACK_FRAMES:
        raise ApplicationInputError("stack result frames exceed the bound")
    normalized: list[dict[str, Any]] = []
    for index, frame in enumerate(frames):
        if not isinstance(frame, Mapping):
            raise ApplicationInputError(f"stack frame {index} must be a mapping")
        frame_id = frame.get("frame_id")
        if type(frame_id) is not int or frame_id < 0:
            raise ApplicationInputError(f"stack frame {index} frame_id is invalid")
        normalized.append(
            {
                "index": frame_id,
                "function": _bounded_text(
                    frame.get("function") or "", f"frame {index} function",
                    _MAX_NAME_BYTES,
                ),
                "file": _bounded_text(
                    frame.get("script") or "", f"frame {index} script",
                    _MAX_SHORT_TEXT_BYTES,
                ),
                "line": _positive_int(frame.get("line"), f"frame {index} line"),
                "is_current": (
                    True if frame.get("is_current") is True else False
                ),
            }
        )
    return tuple(normalized)


def _normalized_locals(locals_result: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    records = locals_result.get("locals")
    if type(records) is not list:
        raise ApplicationInputError("locals result must carry a locals list")
    if len(records) > _MAX_LOCALS:
        raise ApplicationInputError("locals result exceeds the bound")
    normalized: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise ApplicationInputError(f"local {index} must be a mapping")
        name = record.get("name")
        if type(name) is not str or not name:
            raise ApplicationInputError(f"local {index} requires a name")
        bounded_name = _bounded_text(name, f"local {index} name", _MAX_NAME_BYTES)
        # Producer-side runtime-locals policy (Repair Pass 2): a local whose
        # name is credential-shaped never exposes its summarized value.  The
        # name is preserved; the summary is an explicit redaction marker.
        if is_credential_name(bounded_name):
            summary = _REDACTED_LOCAL_SUMMARY
        else:
            summary = _bounded_summary(
                summarize_value_summary(record.get("value") or {})
            )
        normalized.append({"name": bounded_name, "summary": summary})
    return tuple(normalized)


@dataclass(frozen=True)
class ObservabilityContext:
    """Immutable application identity of one Task-4 observability producer.

    Only live-startable Task-1 source kinds are accepted (replay-only kinds
    never enter a live execution).  ``run_id`` is optional and carried
    verbatim on every emitted event when set.
    """

    session_id: str
    task_id: str
    source_kind: SourceKind
    run_id: Optional[str] = None
    initial_sequence: int = 0

    def __post_init__(self) -> None:
        try:
            validate_session_id(self.session_id)
        except Exception as exc:
            raise ApplicationInputError(
                f"invalid session id: {self.session_id!r}"
            ) from exc
        object.__setattr__(
            self, "task_id", _bounded_text(self.task_id, "task_id", _MAX_TASK_ID_BYTES)
        )
        if type(self.source_kind) is not SourceKind:
            raise ApplicationInputError("source_kind must be a SourceKind")
        if not can_start_new_session(self.source_kind):
            raise ApplicationInputError(
                f"source kind {self.source_kind.value!r} is recorded and "
                "cannot produce live execution events"
            )
        object.__setattr__(
            self,
            "run_id",
            (
                None
                if self.run_id is None
                else _bounded_text(self.run_id, "run_id", _MAX_RUN_ID_BYTES)
            ),
        )
        object.__setattr__(
            self,
            "initial_sequence",
            _nonneg_int(self.initial_sequence, "initial_sequence"),
        )


class SessionObservability:
    """Emit validated Task-1 events from real debugger/patch/source facts.

    Each emission returns the constructed event and appends it to this
    producer's ordered list (and, through the emission authority, to the
    optional authoritative :class:`SessionEventSink`).

    Sequence authority (Repair Pass 3): by default this producer owns a
    private :class:`SessionEventEmitter` starting at
    ``context.initial_sequence``, so standalone tests stay possible.  When a
    shared ``emitter`` is supplied, ALL producers of the session emit
    through it, making the emitter the one authoritative sequence/identity/
    clock owner and the journal-failure gate.  The shared emitter identity
    must match the producer context (fail closed).

    Source/patch content safety (Repair Pass 3): source snapshots whose
    captured content matches the shared credential-shape policy are
    rejected at capture (:mod:`agentic_debugger.application.source_snapshots`)
    and also fail closed here, and the optional ``patch_text`` of a
    ``patch.proposed`` event is omitted when the body matches the policy
    while the patch hash and lifecycle are retained.  Harmless identifiers
    such as ``token_count`` or ``secretary`` never match the policy.
    """

    def __init__(
        self,
        context: ObservabilityContext,
        *,
        clock: Callable[[], str] | None = None,
        sink: SessionEventSink | None = None,
        emitter: SessionEventEmitter | None = None,
    ) -> None:
        if type(context) is not ObservabilityContext:
            raise ApplicationInputError("context must be an ObservabilityContext")
        self._context = context
        self._clock = _validated_clock(clock) if clock is not None else _default_clock
        self._events: list[SessionEvent] = []
        self._emitter = self._resolve_emitter(context, emitter, clock, sink)

    def _resolve_emitter(
        self,
        context: ObservabilityContext,
        emitter: SessionEventEmitter | None,
        clock: Callable[[], str] | None,
        sink: SessionEventSink | None,
    ) -> SessionEventEmitter:
        if emitter is not None:
            if type(emitter) is not SessionEventEmitter:
                raise ApplicationInputError("emitter must be a SessionEventEmitter")
            if (
                emitter.session_id != context.session_id
                or emitter.task_id != context.task_id
                or emitter.source_kind is not context.source_kind
            ):
                raise ApplicationContractError(
                    "shared emitter identity does not match the producer context"
                )
            if clock is not None or sink is not None:
                raise ApplicationInputError(
                    "clock/sink belong to the shared emitter; pass them there"
                )
            if (
                context.run_id is not None
                and emitter.run_id is not None
                and emitter.run_id != context.run_id
            ):
                raise ApplicationContractError(
                    "shared emitter run id does not match the producer context"
                )
            # The shared emitter's run_id is bound by the session owner at
            # ``session.started``, never by a producer at construction time.
            return emitter
        return SessionEventEmitter(
            session_id=context.session_id,
            task_id=context.task_id,
            source_kind=context.source_kind,
            run_id=context.run_id,
            clock=self._clock,
            sink=sink,
            initial_sequence=context.initial_sequence,
        )

    @property
    def context(self) -> ObservabilityContext:
        return self._context

    @property
    def emitter(self) -> SessionEventEmitter:
        """The session's shared emission authority (sequence owner)."""
        return self._emitter

    def events(self) -> Tuple[SessionEvent, ...]:
        """The produced session events in emission (sequence) order."""
        return tuple(self._events)

    def _emit(self, kind: SessionEventKind, payload: Dict[str, Any]) -> SessionEvent:
        event = self._emitter.emit(kind, payload)
        self._events.append(event)
        return event

    # -- debugger ----------------------------------------------------------

    def debugger_started(
        self, script: str, breakpoints: Sequence[str]
    ) -> SessionEvent:
        """One debugger start with the active script and breakpoint identities."""
        breakpoint_tuple = tuple(
            _bounded_text(item, "breakpoints", 512) for item in breakpoints
        )
        return self._emit(
            SessionEventKind.DEBUGGER_STARTED,
            {
                "script": _bounded_text_or_none(
                    script, "script", _MAX_SHORT_TEXT_BYTES
                ),
                "breakpoints": breakpoint_tuple,
            },
        )

    def location_changed(
        self,
        script: Optional[str],
        line: Optional[int],
        function: Optional[str],
        pause_generation: int,
    ) -> SessionEvent:
        """One debugger location change (start/step/next/continue)."""
        if line is not None and (type(line) is not int or line < 1):
            raise ApplicationInputError("line must be a positive integer or None")
        return self._emit(
            SessionEventKind.DEBUGGER_LOCATION_CHANGED,
            {
                "script": _bounded_text_or_none(
                    script, "script", _MAX_SHORT_TEXT_BYTES
                ),
                "line": line,
                "function": _bounded_text_or_none(
                    function, "function", _MAX_NAME_BYTES
                ),
                "pause_generation": _nonneg_int(
                    pause_generation, "pause_generation"
                ),
            },
        )

    def stack_observed(self, stack_result: Mapping[str, Any]) -> SessionEvent:
        """One bounded stack observation from a real stack-summary result.

        ``stack_result`` is the validated ``get_stack_summary`` mapping
        carrying ``pause_generation`` and ``frames`` with ``frame_id``/
        ``script``/``line``/``function``/``is_current`` entries.
        """
        generation = stack_result.get("pause_generation")
        if type(generation) is not int or generation < 0:
            raise ApplicationInputError(
                "stack result requires a non-negative pause_generation"
            )
        return self._emit(
            SessionEventKind.DEBUGGER_STACK_OBSERVED,
            {
                "pause_generation": generation,
                "frames": _normalized_frames(stack_result),
            },
        )

    def locals_observed(self, locals_result: Mapping[str, Any]) -> SessionEvent:
        """One bounded locals observation from a real frame-locals result.

        ``locals_result`` is the validated ``get_frame_locals`` mapping
        carrying ``pause_generation`` and ``locals`` entries of
        ``{name, value}`` where ``value`` is a PDB value summary.
        """
        generation = locals_result.get("pause_generation")
        if type(generation) is not int or generation < 0:
            raise ApplicationInputError(
                "locals result requires a non-negative pause_generation"
            )
        return self._emit(
            SessionEventKind.DEBUGGER_LOCALS_OBSERVED,
            {
                "pause_generation": generation,
                "locals": _normalized_locals(locals_result),
            },
        )

    # -- patch lifecycle ---------------------------------------------------

    def patch_proposed(
        self,
        attempt_index: int,
        patch_sha256: str,
        patch_text: Optional[str] = None,
    ) -> SessionEvent:
        payload: Dict[str, Any] = {
            "attempt_index": _nonneg_int(attempt_index, "attempt_index"),
            "patch_sha256": patch_sha256,
        }
        # Producer-side content policy (Repair Pass 3): a patch body that
        # matches the shared credential-shape policy is withheld from the
        # event while the patch hash and lifecycle are retained.  The body
        # is never silently rewritten.
        if patch_text is not None and not contains_credential_shape(patch_text):
            payload["patch_text"] = patch_text
        return self._emit(SessionEventKind.PATCH_PROPOSED, payload)

    def patch_rejected(self, attempt_index: int, rejection_reason: str) -> SessionEvent:
        return self._emit(
            SessionEventKind.PATCH_REJECTED,
            {
                "attempt_index": _nonneg_int(attempt_index, "attempt_index"),
                "rejection_reason": _bounded_diagnostic(rejection_reason),
            },
        )

    def patch_apply_failed(
        self, attempt_index: int, apply_failure_reason: str
    ) -> SessionEvent:
        return self._emit(
            SessionEventKind.PATCH_APPLY_FAILED,
            {
                "attempt_index": _nonneg_int(attempt_index, "attempt_index"),
                "apply_failure_reason": _bounded_diagnostic(apply_failure_reason),
            },
        )

    def patch_applied(
        self,
        attempt_index: int,
        changed_files: Sequence[str],
        syntax_passed: Optional[bool] = None,
    ) -> SessionEvent:
        if syntax_passed is not None and type(syntax_passed) is not bool:
            raise ApplicationInputError("syntax_passed must be a boolean or None")
        return self._emit(
            SessionEventKind.PATCH_APPLIED,
            {
                "attempt_index": _nonneg_int(attempt_index, "attempt_index"),
                "changed_files": tuple(
                    _bounded_text(item, "changed_files", 512)
                    for item in changed_files
                ),
                "syntax_passed": syntax_passed,
            },
        )

    def patch_reverted(self, attempt_index: int) -> SessionEvent:
        return self._emit(
            SessionEventKind.PATCH_REVERTED,
            {"attempt_index": _nonneg_int(attempt_index, "attempt_index")},
        )

    # -- source snapshots --------------------------------------------------

    def source_snapshot(self, snapshot: Any) -> SessionEvent:
        """One safe source snapshot event from a captured snapshot.

        ``snapshot`` is a :class:`application.source_snapshots.SourceSnapshot`
        (duck-typed here to keep this module free of file-I/O imports).
        The snapshot content must already have passed the producer-side
        credential-shape policy (capture rejects unsafe content); a snapshot
        whose text still matches the policy fails closed here rather than
        leaking into an event.
        """
        text = getattr(snapshot, "text", None)
        if type(text) is not str:
            raise ApplicationInputError(
                "source snapshot requires bounded text content"
            )
        if contains_credential_shape(text):
            raise ApplicationInputError(
                "source snapshot content contains a credential-shaped value; "
                "snapshot withheld"
            )
        payload = snapshot.to_event_payload()
        return self._emit(SessionEventKind.SOURCE_SNAPSHOT, payload)

    # -- diagnosis ---------------------------------------------------------

    def diagnosis_recorded(
        self,
        *,
        text: Optional[str] = None,
        file_path: Optional[str] = None,
        symbol: Optional[str] = None,
        confidence: Optional[str] = None,
    ) -> SessionEvent:
        """One explicitly recorded diagnosis artifact (never chain-of-thought).

        Only already-explicit model/system diagnosis artifacts are accepted;
        the bounded ``text`` is the recorded diagnosis claim, not raw model
        output or private reasoning.
        """
        return self._emit(
            SessionEventKind.DIAGNOSIS_RECORDED,
            {
                "text": _multiline_text_or_none(text, "text", 4096),
                "file_path": _bounded_text_or_none(
                    file_path, "file_path", _MAX_SHORT_TEXT_BYTES
                ),
                "symbol": _bounded_text_or_none(symbol, "symbol", _MAX_NAME_BYTES),
                "confidence": _bounded_text_or_none(
                    confidence, "confidence", _MAX_SHORT_TEXT_BYTES
                ),
            },
        )


__all__ = [
    "ObservabilityContext",
    "SessionObservability",
    "summarize_value_summary",
]
