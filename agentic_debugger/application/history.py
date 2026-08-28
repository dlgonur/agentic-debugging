"""App-owned session history: manifests, discovery, and reopening.

Task 5 owns the application run root, session registration, and manifest
that Task 3 intentionally deferred.  This module provides the smallest
robust filesystem-backed history layer:

- a strict, versioned ``manifest.json`` per app-owned session directory
  (derived from the authoritative durable journal, never fabricated);
- atomic manifest writes (temp file + ``fsync`` + atomic replace);
- one-level discovery of app-owned session directories with honest
  classification (``complete`` / ``interrupted`` / ``malformed`` /
  ``invalid_manifest`` / ``unregistered``);
- read-only reopening of app-owned sessions through the shared replay
  cursor (same presentation reducer as live events);
- read-only adapters for explicitly supported historical formats
  (canonical trajectories, R5 evidence, professor traces) via
  :mod:`agentic_debugger.application.adapters`.

Rules:

- No database, no file watching, no background daemon, no remote history.
- Frozen/historical scientific evidence is never rewritten, normalized,
  appended, renamed, moved, or re-hashed into new meaning.
- Invalid or incomplete manifests never appear as successful sessions; the
  durable journal classification is the truth and the manifest is store
  metadata derived from it.
- Missing historical fields stay absent/``None`` (the ``NOT RECORDED``
  display rule); nothing is reconstructed from the current checkout.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from agentic_debugger.application import (
    ApplicationError,
    ApplicationInputError,
)
from agentic_debugger.application.events import (
    SessionEvent,
    SessionEventKind,
    SessionStatus,
    SessionTerminationReason,
    SourceKind,
    compatible_reasons,
    validate_session_id,
    validate_utc_timestamp,
)
from agentic_debugger.application.journal import (
    JournalReadState,
    read_session_journal,
)
from agentic_debugger.application.replay import SessionReplaySource

MANIFEST_SCHEMA_VERSION = "app-session-manifest-v1"
RUNS_DIR_NAME = "runs"
JOURNAL_FILE_NAME = "session.events.jsonl"
RESULT_FILE_NAME = "result.json"
DEFAULT_HISTORY_DIR_NAME = "AgenticDebugger"

_MAX_MANIFEST_ARTIFACTS = 64
_MAX_TEXT_BYTES = 512


def default_history_root() -> Path:
    """Return the platform-local root used for app-owned session history.

    This path helper lives in the UI-independent application layer so
    headless history/reporting commands do not need to import Textual.
    """

    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / DEFAULT_HISTORY_DIR_NAME


class HistoryError(ApplicationError):
    """Base class for app-owned history failures."""


class HistoryInputError(HistoryError, ApplicationInputError):
    """Raised for malformed history input (manifest, path, format)."""


class HistoryNotFoundError(HistoryError):
    """Raised when a session/recorded source does not exist."""


class HistoryClassification(str, Enum):
    """Honest classification of one app-owned session directory.

    Only ``COMPLETE`` may represent a successful session: it requires a
    valid manifest AND a complete valid journal AND a manifest that still
    describes the current authoritative journal/artifacts (a stale or
    tampered manifest is ``INVALID_MANIFEST``, never success).  Everything
    else is interrupted, malformed, invalid, or unregistered -- never
    success.
    """

    COMPLETE = "complete"
    INTERRUPTED = "interrupted"
    MALFORMED = "malformed"
    INVALID_MANIFEST = "invalid_manifest"
    UNREGISTERED = "unregistered"

    @property
    def is_success(self) -> bool:
        return self is HistoryClassification.COMPLETE


def _bounded_text(value: Any, label: str, max_chars: int) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise HistoryInputError(f"{label} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise HistoryInputError(f"{label} must be UTF-8 text")
    if len(encoded) > max_chars:
        raise HistoryInputError(f"{label} exceeds the {max_chars}-byte bound")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise HistoryInputError(f"{label} contains control characters")
    return value


def _bounded_text_or_none(value: Any, label: str, max_chars: int) -> Optional[str]:
    if value is None:
        return None
    return _bounded_text(value, label, max_chars)


def _sha256_hex(value: Any, label: str) -> str:
    if type(value) is not str or len(value) != 64:
        raise HistoryInputError(f"{label} must be a 64-character hex digest")
    try:
        int(value, 16)
    except ValueError:
        raise HistoryInputError(f"{label} must be a hex digest") from None
    return value


def _optional_utc(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    try:
        return validate_utc_timestamp(value)
    except Exception as exc:
        raise HistoryInputError(f"{label} is invalid") from exc


def _relative_safe_path(value: Any, label: str) -> str:
    path = _bounded_text(value, label, 2048)
    if path.startswith("/") or path.startswith("\\"):
        raise HistoryInputError(f"{label} must be a relative path")
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        raise HistoryInputError(f"{label} must not carry a drive letter")
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    if ".." in parts:
        raise HistoryInputError(f"{label} must not contain .. traversal")
    return path.replace("\\", "/")


def _optional_enum(value: Any, label: str, enum_type: type) -> Optional[Any]:
    if value is None:
        return None
    if type(value) is enum_type:
        return value
    if type(value) is not str or not value:
        raise HistoryInputError(f"{label} must be a string or null")
    try:
        return enum_type(value)
    except ValueError:
        raise HistoryInputError(f"{label} is not a valid {enum_type.__name__}") from None


# ---------------------------------------------------------------------------
# Manifest model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestArtifact:
    """One app-owned artifact reference with its content hash."""

    path: str
    sha256: str


@dataclass(frozen=True)
class SessionManifest:
    """Strict, versioned manifest of one app-owned session directory.

    Every field is validated fail-closed.  ``status``/``termination_reason``
    are null when the durable journal recorded no terminal event (an
    interrupted session); they are never invented.
    """

    schema_version: str
    session_id: str
    task_id: str
    source_kind: SourceKind
    run_id: Optional[str] = None
    started_at_utc: Optional[str] = None
    ended_at_utc: Optional[str] = None
    status: Optional[SessionStatus] = None
    termination_reason: Optional[SessionTerminationReason] = None
    config_fingerprint: Optional[str] = None
    cleanup_verified: bool = False
    journal_path: str = JOURNAL_FILE_NAME
    journal_sha256: str = ""
    artifacts: Tuple[ManifestArtifact, ...] = ()
    verifier_status: Optional[str] = None
    verifier_outcome: Optional[str] = None
    retry_of_session_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise HistoryInputError(
                f"unsupported manifest schema version: {self.schema_version!r}"
            )
        validate_session_id(self.session_id)
        _bounded_text(self.task_id, "task_id", 256)
        if type(self.source_kind) is not SourceKind:
            raise HistoryInputError("source_kind must be a SourceKind")
        object.__setattr__(
            self, "run_id", _bounded_text_or_none(self.run_id, "run_id", 256)
        )
        object.__setattr__(
            self, "started_at_utc", _optional_utc(self.started_at_utc, "started_at_utc")
        )
        object.__setattr__(
            self, "ended_at_utc", _optional_utc(self.ended_at_utc, "ended_at_utc")
        )
        object.__setattr__(self, "status", _optional_enum(self.status, "status", SessionStatus))
        object.__setattr__(
            self,
            "termination_reason",
            _optional_enum(
                self.termination_reason, "termination_reason", SessionTerminationReason
            ),
        )
        if self.status is not None and not self.status.terminal:
            raise HistoryInputError("manifest status must be terminal or null")
        if self.termination_reason is not None:
            if self.status is None:
                raise HistoryInputError(
                    "manifest termination_reason requires a terminal status"
                )
            if self.termination_reason not in compatible_reasons(self.status):
                raise HistoryInputError(
                    "manifest termination_reason is not compatible with the status"
                )
        if self.config_fingerprint is not None:
            _sha256_hex(self.config_fingerprint, "config_fingerprint")
        if type(self.cleanup_verified) is not bool:
            raise HistoryInputError("cleanup_verified must be a boolean")
        object.__setattr__(
            self, "journal_path", _relative_safe_path(self.journal_path, "journal_path")
        )
        _sha256_hex(self.journal_sha256, "journal_sha256")
        if type(self.artifacts) is not tuple or len(self.artifacts) > _MAX_MANIFEST_ARTIFACTS:
            raise HistoryInputError("artifacts must be a bounded tuple")
        for index, artifact in enumerate(self.artifacts):
            if type(artifact) is not ManifestArtifact:
                raise HistoryInputError(f"artifacts[{index}] must be a ManifestArtifact")
            _relative_safe_path(artifact.path, f"artifacts[{index}].path")
            _sha256_hex(artifact.sha256, f"artifacts[{index}].sha256")
        object.__setattr__(
            self,
            "verifier_status",
            _bounded_text_or_none(self.verifier_status, "verifier_status", _MAX_TEXT_BYTES),
        )
        object.__setattr__(
            self,
            "verifier_outcome",
            _bounded_text_or_none(self.verifier_outcome, "verifier_outcome", _MAX_TEXT_BYTES),
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "source_kind": self.source_kind.value,
            "run_id": self.run_id,
            "started_at_utc": self.started_at_utc,
            "ended_at_utc": self.ended_at_utc,
            "status": self.status.value if self.status is not None else None,
            "termination_reason": (
                self.termination_reason.value if self.termination_reason is not None else None
            ),
            "config_fingerprint": self.config_fingerprint,
            "cleanup_verified": self.cleanup_verified,
            "journal_path": self.journal_path,
            "journal_sha256": self.journal_sha256,
            "artifacts": [
                {"path": artifact.path, "sha256": artifact.sha256}
                for artifact in self.artifacts
            ],
            "verifier_status": self.verifier_status,
            "verifier_outcome": self.verifier_outcome,
            "retry_of_session_id": self.retry_of_session_id,
        }


def validate_manifest_mapping(m: Any) -> SessionManifest:
    """Strictly validate one manifest mapping; fails closed on any violation."""
    if not isinstance(m, Mapping):
        raise HistoryInputError("manifest must be a mapping")
    known = {
        "schema_version", "session_id", "task_id", "source_kind", "run_id",
        "started_at_utc", "ended_at_utc", "status", "termination_reason",
        "config_fingerprint", "cleanup_verified", "journal_path",
        "journal_sha256", "artifacts", "verifier_status", "verifier_outcome",
        "retry_of_session_id",
    }
    missing = known - set(m.keys())
    if missing:
        raise HistoryInputError(f"manifest is missing fields: {sorted(missing)}")
    extra = set(m.keys()) - known
    if extra:
        raise HistoryInputError(f"manifest has unknown fields: {sorted(extra)}")
    artifacts = m["artifacts"]
    if type(artifacts) is not list:
        raise HistoryInputError("manifest artifacts must be a list")
    parsed_artifacts = []
    for index, item in enumerate(artifacts):
        if not isinstance(item, Mapping):
            raise HistoryInputError(f"manifest artifacts[{index}] must be a mapping")
        artifact_known = {"path", "sha256"}
        if set(item.keys()) != artifact_known:
            raise HistoryInputError(f"manifest artifacts[{index}] fields are invalid")
        parsed_artifacts.append(
            ManifestArtifact(
                path=_relative_safe_path(item["path"], f"artifacts[{index}].path"),
                sha256=_sha256_hex(item["sha256"], f"artifacts[{index}].sha256"),
            )
        )
    return SessionManifest(
        schema_version=m["schema_version"],
        session_id=m["session_id"],
        task_id=m["task_id"],
        source_kind=SourceKind(m["source_kind"]),
        run_id=m["run_id"],
        started_at_utc=m["started_at_utc"],
        ended_at_utc=m["ended_at_utc"],
        status=m["status"],
        termination_reason=m["termination_reason"],
        config_fingerprint=m["config_fingerprint"],
        cleanup_verified=m["cleanup_verified"],
        journal_path=m["journal_path"],
        journal_sha256=m["journal_sha256"],
        artifacts=tuple(parsed_artifacts),
        verifier_status=m["verifier_status"],
        verifier_outcome=m["verifier_outcome"],
        retry_of_session_id=m["retry_of_session_id"],
    )


# ---------------------------------------------------------------------------
# Atomic JSON writes
# ---------------------------------------------------------------------------


def atomic_write_json(path: str | os.PathLike[str], mapping: Dict[str, Any]) -> None:
    """Atomically write one JSON document (temp + fsync + atomic replace).

    On failure the previous file (if any) stays untouched and the temp file
    is removed.  Never raises on partial state: every failure raises
    :class:`HistoryError`.
    """
    destination = Path(path)
    tmp = destination.with_name(destination.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(mapping, stream, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, destination)
    except OSError as exc:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise HistoryError(
            f"atomic manifest write failed at {destination}: {exc}"
        ) from exc


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# History entries and store
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionHistoryEntry:
    """One honest listing entry for an app-owned session directory."""

    session_id: Optional[str]
    classification: HistoryClassification
    directory: Optional[str] = None
    task_id: Optional[str] = None
    source_kind: Optional[SourceKind] = None
    run_id: Optional[str] = None
    status: Optional[SessionStatus] = None
    termination_reason: Optional[SessionTerminationReason] = None
    started_at_utc: Optional[str] = None
    ended_at_utc: Optional[str] = None
    config_fingerprint: Optional[str] = None
    cleanup_verified: Optional[bool] = None
    verifier_status: Optional[str] = None
    verifier_outcome: Optional[str] = None
    retry_of_session_id: Optional[str] = None
    manifest_path: Optional[str] = None
    journal_path: Optional[str] = None
    note: Optional[str] = None

    @property
    def is_success(self) -> bool:
        return self.classification.is_success


@dataclass(frozen=True)
class ReopenedSession:
    """One reopened app-owned session: its entry and read-only replay source."""

    entry: SessionHistoryEntry
    replay: SessionReplaySource


class HistoryStore:
    """App-owned session history: registration, listing, and reopening.

    The store owns the run root layout only; it never mutates frozen or
    scientific evidence, and it never scans directories recursively.

    Containment (Repair Pass 3): ``register()`` may mutate only an app-owned
    session directory belonging to this store -- an immediate child of the
    store's ``runs/`` root whose resolved path stays inside it.  Arbitrary
    external directories, siblings outside the runs root, traversal, and
    symlink/reparse-point escapes all fail closed, so frozen evidence can
    never be registered in place.

    Read containment (Repair Pass 2): the same app-owned boundary applies
    before any directory is read as this store's history.  ``list_sessions()``
    skips a child whose resolved location is not a genuine immediate child of
    ``runs/`` (an escaping symlink/reparse point), and ``reopen()`` fails
    closed on such a directory -- external journal/manifest contents are
    never classified as app-owned evidence, never presented as COMPLETE, and
    never modified.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        runs_dir_name: str = RUNS_DIR_NAME,
    ) -> None:
        if type(runs_dir_name) is not str or not runs_dir_name:
            raise HistoryInputError("runs_dir_name must be a non-empty string")
        if (
            "/" in runs_dir_name
            or "\\" in runs_dir_name
            or runs_dir_name in (".", "..")
        ):
            raise HistoryInputError(
                "runs_dir_name must be a plain directory name without separators"
            )
        self._root = Path(root).resolve()
        self._runs_dir_name = runs_dir_name
        self._runs_dir = self._root / runs_dir_name

    @property
    def root(self) -> Path:
        return self._root

    @property
    def runs_dir(self) -> Path:
        return self._runs_dir

    def session_dir(self, session_id: str) -> Path:
        """Path of one app-owned session directory (does not create it)."""
        validate_session_id(session_id)
        return self._runs_dir / session_id

    # -- registration --------------------------------------------------------

    def register(
        self,
        session_dir: str | os.PathLike[str],
        *,
        write_result: bool = True,
    ) -> SessionHistoryEntry:
        """Validate one session directory, derive + atomically write its
        manifest, and return the classified entry.

        The durable journal is the truth: the manifest is derived from it and
        never fabricates unrecorded fields.  Idempotent: re-registering an
        already-registered directory rewrites the manifest from the journal.

        The directory must be an app-owned session directory of THIS store:
        an immediate, symlink-resolved child of the store's ``runs/`` root
        whose name is the journal's session id.  Any other directory
        (external, sibling outside ``runs/``, traversal, or symlink escape)
        fails closed and is never written to.
        """
        directory = Path(session_dir).resolve()
        if not directory.is_dir():
            raise HistoryNotFoundError(f"session directory does not exist: {directory}")
        self._require_app_owned_session_dir(directory)
        journal_path = directory / JOURNAL_FILE_NAME
        read = read_session_journal(journal_path)
        events = read.events
        if not events:
            raise HistoryInputError(
                "session journal is empty or missing; nothing to register"
            )
        dir_name = directory.name
        try:
            validate_session_id(dir_name)
        except Exception as exc:
            raise HistoryInputError(
                f"session directory name is not a valid session id: {dir_name!r}"
            ) from exc
        if events[0].session_id != dir_name:
            raise HistoryInputError(
                "session directory name does not match the journal session id"
            )
        manifest = _derive_manifest(directory, read)
        if write_result:
            result_mapping = _derive_result_mapping(events)
            if result_mapping is not None:
                atomic_write_json(directory / RESULT_FILE_NAME, result_mapping)
                manifest = _derive_manifest(directory, read)
        atomic_write_json(directory / "manifest.json", manifest.to_mapping())
        return self._classify(directory, read, manifest)

    def _require_app_owned_session_dir(self, directory: Path) -> None:
        """Fail closed unless ``directory`` is an immediate child of runs/.

        The resolved directory must be an immediate child of the store's
        resolved ``runs/`` root.  ``Path.resolve()`` follows symlinks and
        reparse points, so an escaping link resolves outside the runs root
        and fails here instead of mutating external evidence.
        """
        if not self._is_app_owned_child(directory):
            raise HistoryInputError(
                "session directory must be an immediate app-owned child of "
                f"the store's runs root: {self._runs_dir}"
            )

    def _is_app_owned_child(self, child: Path) -> bool:
        """Whether ``child`` is a genuine immediate child of this store's
        ``runs/`` root (Repair Pass 2 read-containment rule).

        Both the unresolved name and the resolved location must agree: the
        resolved directory must be an immediate child of the resolved runs
        root AND keep the child's own name.  An escaping symlink/reparse
        point (external directory, or an internal link to another session)
        therefore never classifies or reads as this store's app-owned
        session evidence.  This is a pure path check: no external bytes are
        read and nothing is mutated.
        """
        runs_resolved = self._runs_dir.resolve()
        resolved = child.resolve()
        return resolved.parent == runs_resolved and resolved.name == child.name

    # -- discovery -----------------------------------------------------------

    def list_sessions(self) -> Tuple[SessionHistoryEntry, ...]:
        """List every one-level app-owned session directory with honest
        classification.  Directories without a manifest or journal are not
        session evidence and are skipped; a child whose resolved location
        escapes this store's ``runs/`` root (symlink/reparse escape) is
        skipped without reading its external contents."""
        if not self._runs_dir.is_dir():
            return ()
        entries: list[SessionHistoryEntry] = []
        for child in sorted(self._runs_dir.iterdir(), key=lambda p: p.name):
            if not child.is_dir():
                continue
            if not self._is_app_owned_child(child):
                continue
            has_manifest = (child / "manifest.json").is_file()
            has_journal = (child / JOURNAL_FILE_NAME).is_file()
            if not has_manifest and not has_journal:
                continue
            try:
                entry = self._classify_directory(child)
            except Exception as exc:
                entries.append(
                    SessionHistoryEntry(
                        session_id=child.name,
                        classification=HistoryClassification.INVALID_MANIFEST,
                        directory=str(child),
                        note=f"classification failed: {_bounded_diagnostic(exc)}",
                    )
                )
                continue
            entries.append(entry)
        return tuple(entries)

    def _classify_directory(self, directory: Path) -> SessionHistoryEntry:
        manifest_path = directory / "manifest.json"
        manifest: Optional[SessionManifest] = None
        if manifest_path.is_file():
            try:
                manifest = validate_manifest_mapping(
                    json.loads(manifest_path.read_text(encoding="utf-8"))
                )
            except Exception as exc:
                return SessionHistoryEntry(
                    session_id=directory.name,
                    classification=HistoryClassification.INVALID_MANIFEST,
                    directory=str(directory),
                    manifest_path=str(manifest_path),
                    note=f"invalid manifest: {_bounded_diagnostic(exc)}",
                )
        read = read_session_journal(directory / JOURNAL_FILE_NAME)
        return self._classify(directory, read, manifest)

    def _classify(
        self,
        directory: Path,
        read: Any,
        manifest: Optional[SessionManifest],
    ) -> SessionHistoryEntry:
        events = read.events
        if manifest is not None:
            # Provenance consistency: the store-owned manifest must agree
            # with the authoritative journal identity.  A manifest that
            # disagrees (tampered or stale) is invalid and can never make
            # the session appear successful under a wrong identity.
            if events:
                journal_session = events[0].session_id
                journal_task = events[0].task_id
                journal_kind = events[0].source_kind
                if (
                    manifest.session_id != journal_session
                    or manifest.task_id != journal_task
                    or manifest.source_kind is not journal_kind
                ):
                    return SessionHistoryEntry(
                        session_id=directory.name,
                        classification=HistoryClassification.INVALID_MANIFEST,
                        directory=str(directory),
                        manifest_path=str(directory / "manifest.json"),
                        journal_path=str(directory / JOURNAL_FILE_NAME),
                        note=(
                            "manifest identity does not match the durable "
                            "journal"
                        ),
                    )
            base = SessionHistoryEntry(
                session_id=manifest.session_id,
                classification=HistoryClassification.UNREGISTERED,
                task_id=manifest.task_id,
                source_kind=manifest.source_kind,
                run_id=manifest.run_id,
                status=manifest.status,
                termination_reason=manifest.termination_reason,
                started_at_utc=manifest.started_at_utc,
                ended_at_utc=manifest.ended_at_utc,
                config_fingerprint=manifest.config_fingerprint,
                cleanup_verified=manifest.cleanup_verified,
                verifier_status=manifest.verifier_status,
                verifier_outcome=manifest.verifier_outcome,
                retry_of_session_id=manifest.retry_of_session_id,
                manifest_path=str(directory / "manifest.json"),
                journal_path=str(directory / JOURNAL_FILE_NAME),
            )
        else:
            base = SessionHistoryEntry(
                session_id=events[0].session_id if events else directory.name,
                classification=HistoryClassification.UNREGISTERED,
                journal_path=str(directory / JOURNAL_FILE_NAME),
            )
        if read.state is JournalReadState.MALFORMED or read.state is JournalReadState.MISSING:
            classification = HistoryClassification.MALFORMED
            note = read.error or "journal is malformed or missing"
        elif read.state is JournalReadState.INTERRUPTED:
            classification = HistoryClassification.INTERRUPTED
            note = read.error or "journal ends without a terminal event"
        elif manifest is None:
            # A complete journal without a manifest is unregistered app-owned
            # evidence; it is never presented as a successful session.
            classification = HistoryClassification.UNREGISTERED
            note = "complete journal without a registered manifest"
        else:
            # COMPLETE means the manifest actually describes the current
            # authoritative journal and referenced artifacts.  A stale or
            # tampered manifest (journal hash, terminal state, run id,
            # timestamps, verifier metadata, or artifact hashes) is
            # INVALID_MANIFEST and can never remain COMPLETE.  Interrupted
            # and malformed journals are already honest non-success
            # classifications dominated by the journal truth, so they do not
            # need this comparison.
            mismatch = _manifest_mismatch(directory, manifest, read)
            if mismatch is not None:
                classification = HistoryClassification.INVALID_MANIFEST
                note = mismatch
            else:
                classification = HistoryClassification.COMPLETE
                note = None
        if classification is HistoryClassification.COMPLETE:
            return replace(base, classification=classification, directory=str(directory))
        return replace(
            base,
            classification=classification,
            directory=str(directory),
            note=note,
        )

    # -- reopening -----------------------------------------------------------

    def reopen(self, session_id: str) -> ReopenedSession:
        """Reopen one app-owned session read-only.

        Validates the manifest (when present), classifies the journal, and
        returns the replay cursor over the validated events.  Malformed or
        missing journals and invalid manifests fail closed (they can never be
        replayed as successful sessions).  The session directory must be a
        genuine app-owned immediate child of this store's ``runs/`` root: a
        symlink/reparse-point escape fails closed before any external
        manifest or journal content is read.
        """
        directory = self.session_dir(session_id)
        if not directory.is_dir():
            raise HistoryNotFoundError(f"session directory does not exist: {directory}")
        if not self._is_app_owned_child(directory):
            raise HistoryError(
                "session directory is not an app-owned immediate child of "
                f"the store's runs root: {directory}"
            )
        manifest_path = directory / "manifest.json"
        manifest: Optional[SessionManifest] = None
        if manifest_path.is_file():
            try:
                manifest = validate_manifest_mapping(
                    json.loads(manifest_path.read_text(encoding="utf-8"))
                )
            except Exception as exc:
                raise HistoryInputError(
                    f"manifest is invalid: {_bounded_diagnostic(exc)}"
                ) from exc
        read = read_session_journal(directory / JOURNAL_FILE_NAME)
        if read.state is JournalReadState.MALFORMED or read.state is JournalReadState.MISSING:
            raise HistoryError(
                f"session journal cannot be replayed "
                f"({read.state.value}): {read.error or 'no durable events'}"
            )
        if not read.events:
            raise HistoryError("session journal contains no durable events")
        entry = self._classify(directory, read, manifest)
        if entry.classification is HistoryClassification.INVALID_MANIFEST:
            raise HistoryInputError(
                f"manifest is invalid: {entry.note or 'identity mismatch'}"
            )
        replay = SessionReplaySource(
            events=read.events,
            source_kind=read.events[0].source_kind,
            task_id=read.events[0].task_id,
            session_id=read.events[0].session_id,
        )
        return ReopenedSession(entry=entry, replay=replay)

    def open_recorded(
        self,
        path: str | os.PathLike[str],
        *,
        format: Optional[str] = None,
        clock: Optional[Callable[[], str]] = None,
    ) -> Any:
        """Open one explicitly supported recorded evidence file read-only.

        Dispatches to the historical adapters; the source file is never
        modified.  Only known formats are accepted (canonical trajectory,
        R5 evidence, professor trace); other evidence fails closed.
        """
        from agentic_debugger.application.adapters import open_recorded_file

        return open_recorded_file(path, format=format, clock=clock)


def _bounded_diagnostic(text: str) -> str:
    cleaned = "".join(
        char if 0x20 <= ord(char) != 0x7F else " " for char in str(text)
    )
    if len(cleaned) > 400:
        cleaned = cleaned[:397] + "..."
    return cleaned or "unspecified"


def _manifest_mismatch(
    directory: Path, manifest: SessionManifest, read: Any
) -> Optional[str]:
    """One canonical manifest-integrity comparison (Repair Pass 3).

    Re-derives the expected manifest strictly from the current authoritative
    journal and referenced artifacts and compares it field-for-field with
    the recorded manifest.  ``None`` means the manifest still describes the
    current journal/artifacts exactly (the registration-time derivation);
    any mismatch -- journal SHA-256, identity, run id, terminal status/
    reason, start/end timestamps, cleanup state, verifier metadata,
    configuration fingerprint, or referenced artifact presence/hash --
    returns a bounded mismatch note.  Registration remains the only explicit
    mutation action; this comparison never rewrites anything.
    """
    events = read.events
    if not events:
        return "manifest has no authoritative journal to describe"
    try:
        expected = _derive_manifest(directory, read)
    except Exception as exc:
        return f"cannot derive the expected manifest: {_bounded_diagnostic(exc)}"
    if expected.to_mapping() != manifest.to_mapping():
        return (
            "manifest does not match the current authoritative "
            "journal/artifacts"
        )
    return None


def _derive_manifest(directory: Path, read: Any) -> SessionManifest:
    """Derive the store-owned manifest strictly from the durable journal."""
    events = read.events
    terminal_event: Optional[SessionEvent] = None
    started_at: Optional[str] = None
    run_id: Optional[str] = None
    cleanup_verified = False
    config_fingerprint: Optional[str] = None
    verifier_status: Optional[str] = None
    verifier_outcome: Optional[str] = None
    retry_of_session_id: Optional[str] = None
    for event in events:
        if event.event_kind is SessionEventKind.SESSION_CREATED:
            config_fingerprint = event.payload.get("spec_fingerprint")
            linkage = event.payload.get("retry_of_session_id")
            if isinstance(linkage, str) and linkage:
                retry_of_session_id = linkage
        elif event.event_kind is SessionEventKind.SESSION_STARTED:
            run_id = event.run_id
            started_at = event.timestamp_utc
        elif event.event_kind is SessionEventKind.CLEANUP_COMPLETED:
            cleanup_verified = bool(event.payload["verified"])
        elif event.event_kind is SessionEventKind.VERIFIER_COMPLETED:
            verifier_status = event.payload.get("status")
            verifier_outcome = event.payload.get("outcome")
        elif event.event_kind in (
            SessionEventKind.SESSION_COMPLETED,
            SessionEventKind.SESSION_FAILED,
            SessionEventKind.SESSION_CANCELLED,
        ):
            terminal_event = event
    status: Optional[SessionStatus] = None
    reason: Optional[SessionTerminationReason] = None
    ended_at: Optional[str] = None
    if terminal_event is not None:
        status = SessionStatus(terminal_event.payload["status"])
        reason = SessionTerminationReason(terminal_event.payload["termination_reason"])
        ended_at = terminal_event.timestamp_utc
    journal_path = directory / JOURNAL_FILE_NAME
    journal_sha256 = _file_sha256(journal_path)
    artifacts: list[ManifestArtifact] = []
    for name in (
        RESULT_FILE_NAME,
        "candidate.patch",
        "local_project_task.json",
        "local_project_verification.json",
        "evaluation.json",
        "operator.command.json",
        "operator.process.json",
        "operator.stdout.txt",
        "operator.stderr.txt",
    ):
        artifact_path = directory / name
        if artifact_path.is_file():
            artifacts.append(
                ManifestArtifact(path=name, sha256=_file_sha256(artifact_path))
            )
    return SessionManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        session_id=events[0].session_id,
        task_id=events[0].task_id,
        source_kind=events[0].source_kind,
        run_id=run_id,
        started_at_utc=started_at,
        ended_at_utc=ended_at,
        status=status,
        termination_reason=reason,
        config_fingerprint=config_fingerprint,
        cleanup_verified=cleanup_verified,
        journal_path=JOURNAL_FILE_NAME,
        journal_sha256=journal_sha256,
        artifacts=tuple(artifacts),
        verifier_status=verifier_status,
        verifier_outcome=verifier_outcome,
        retry_of_session_id=retry_of_session_id,
    )


def _derive_result_mapping(events: Tuple[SessionEvent, ...]) -> Optional[Dict[str, Any]]:
    """Derive the terminal ``SessionResult``-shaped mapping from the journal.

    App-owned convenience artifact; the durable journal remains authoritative.
    Returns None when no terminal event was recorded (interrupted session).
    """
    terminal_event: Optional[SessionEvent] = None
    started_at: Optional[str] = None
    run_id: Optional[str] = None
    cleanup_verified = False
    diagnostics: list[str] = []
    for event in events:
        if event.event_kind is SessionEventKind.SESSION_STARTED:
            run_id = event.run_id
            started_at = event.timestamp_utc
        elif event.event_kind is SessionEventKind.CLEANUP_COMPLETED:
            cleanup_verified = bool(event.payload["verified"])
        elif event.event_kind is SessionEventKind.DIAGNOSIS_RECORDED:
            text = event.payload.get("text")
            if isinstance(text, str) and text:
                diagnostics.append(text)
        elif event.event_kind in (
            SessionEventKind.SESSION_COMPLETED,
            SessionEventKind.SESSION_FAILED,
            SessionEventKind.SESSION_CANCELLED,
        ):
            terminal_event = event
    if terminal_event is None:
        return None
    return {
        "session_id": events[0].session_id,
        "task_id": events[0].task_id,
        "source_kind": events[0].source_kind.value,
        "status": terminal_event.payload["status"],
        "termination_reason": terminal_event.payload["termination_reason"],
        "run_id": run_id,
        "started_at_utc": started_at,
        "ended_at_utc": terminal_event.timestamp_utc,
        "sequence": terminal_event.sequence,
        "cleanup_verified": cleanup_verified,
        "diagnostics": diagnostics[:4],
    }


__all__ = [
    "HistoryClassification",
    "HistoryError",
    "HistoryInputError",
    "HistoryNotFoundError",
    "HistoryStore",
    "JOURNAL_FILE_NAME",
    "MANIFEST_SCHEMA_VERSION",
    "ManifestArtifact",
    "ReopenedSession",
    "RESULT_FILE_NAME",
    "RUNS_DIR_NAME",
    "SessionHistoryEntry",
    "SessionManifest",
    "atomic_write_json",
    "validate_manifest_mapping",
]
